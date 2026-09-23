"""S5 event-transaction barrier. No new content, event, or graph tables.

Revision ID: 011; Revises: 010.
"""

from alembic import op

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE INDEX maintenance_watch_sources ON knowledge_artifacts
      (account_id, (payload->'source'->>'context_id')) WHERE kind='maintenance_watch'
    """)
    op.execute("""
    CREATE FUNCTION s5_bind_event() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE watches jsonb;
    BEGIN
      IF NEW.change_type <> 'modified' THEN RETURN NEW; END IF;
      SELECT jsonb_agg(jsonb_build_object('account_id',a.account_id,'id',a.id,
               'kind',a.kind,'sha256',a.sha256) ORDER BY a.id)
        INTO watches FROM knowledge_artifacts a
        WHERE a.account_id=NEW.account_id AND a.kind='maintenance_watch'
          AND a.payload->'source'->>'context_id'=NEW.context_id::text;
      IF watches IS NOT NULL THEN
        NEW.metadata := coalesce(NEW.metadata,'{}'::jsonb) || jsonb_build_object('s5_watches',watches);
      END IF;
      RETURN NEW;
    END $$
    """)
    op.execute("""
    CREATE TRIGGER zz_s5_bind_event BEFORE INSERT ON change_events
      FOR EACH ROW EXECUTE FUNCTION s5_bind_event()
    """)
    op.execute("""
    CREATE FUNCTION s5_protect_event() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE targets uuid[];
    BEGIN
      IF NEW.metadata->'s5_watches' IS NULL THEN RETURN NEW; END IF;
      WITH RECURSIVE affected(id) AS (
        SELECT (n->>'context_id')::uuid
          FROM jsonb_array_elements(NEW.metadata->'s5_watches') w
          JOIN knowledge_artifacts a ON a.account_id=NEW.account_id AND a.id=(w->>'id')::uuid,
          LATERAL jsonb_array_elements(a.payload->'affected') n
        UNION
        SELECT e.id FROM affected i JOIN LATERAL (
          SELECT d.dependent_id id FROM dependencies d WHERE d.dependency_id=i.id
          UNION SELECT r.context_id FROM context_relations r WHERE r.related_context_id=i.id
        ) e ON true
      ) SELECT array_agg(c.id ORDER BY c.id) INTO targets FROM affected i
        JOIN contexts c ON c.id=i.id AND c.account_id=NEW.account_id
        WHERE c.id<>NEW.context_id;
      UPDATE contexts SET
        status=CASE WHEN status='active' AND validity_status='fresh' THEN 'stale' ELSE status END,
        validity_status=CASE WHEN validity_status='fresh' THEN 'stale' ELSE validity_status END,
        validity_reason=CASE WHEN validity_status='fresh' THEN 's5:source_changed' ELSE validity_reason END,
        stale_at=coalesce(stale_at,now()), updated_at=now()
        WHERE account_id=NEW.account_id AND id=ANY(targets);
      INSERT INTO context_invalidations(context_id,cause_event_id,source_context_id,source_version,reason_hash)
        SELECT unnest(targets),NEW.event_id,NEW.context_id,NEW.source_version,
          encode(digest('s5:source_changed','sha256'),'hex') ON CONFLICT DO NOTHING;
      INSERT INTO propagation_trace(event_id,trace_type,payload)
        VALUES(NEW.event_id,'s5_barrier',jsonb_build_object('account_id',NEW.account_id,
          'root_event_id',NEW.root_event_id,'watches',NEW.metadata->'s5_watches',
          'affected',coalesce(to_jsonb(targets),'[]'::jsonb),'source_version',NEW.source_version,
          'barrier_at',clock_timestamp(),'initialization','pending'));
      RETURN NEW;
    END $$
    """)
    op.execute("""
    CREATE TRIGGER s5_protect_event AFTER INSERT ON change_events
      FOR EACH ROW EXECUTE FUNCTION s5_protect_event();
    """)


def downgrade():
    # Never remove unresolved causes or make protected contexts fresh.
    op.execute("DROP TRIGGER s5_protect_event ON change_events")
    op.execute("DROP TRIGGER zz_s5_bind_event ON change_events")
    op.execute("DROP FUNCTION s5_protect_event()")
    op.execute("DROP FUNCTION s5_bind_event()")
    op.execute("DROP INDEX maintenance_watch_sources")
