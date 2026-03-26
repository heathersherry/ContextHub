/**
 * ContextHubBridge — ContextEngine implementation that forwards lifecycle
 * calls to the ContextHub Python sidecar over HTTP.
 *
 * Type shapes here mirror the ContextEngine contract defined in
 * openclaw/src/context-engine/types.ts. At runtime inside the OpenClaw
 * process, `delegateCompactionToRuntime` is resolved via dynamic import
 * from "openclaw/plugin-sdk".
 */

// ---------------------------------------------------------------------------
// Minimal type surface matching OpenClaw ContextEngine contract.
// Kept inline so the bridge compiles standalone (no build-time dep on the
// OpenClaw monorepo). Shapes verified against openclaw/src/context-engine/types.ts.
// ---------------------------------------------------------------------------

export type ContextEngineInfo = {
  id: string;
  name: string;
  version?: string;
  ownsCompaction?: boolean;
};

export type AssembleResult = {
  messages: unknown[];
  estimatedTokens: number;
  systemPromptAddition?: string;
};

export type CompactResult = {
  ok: boolean;
  compacted: boolean;
  reason?: string;
  result?: {
    summary?: string;
    firstKeptEntryId?: string;
    tokensBefore: number;
    tokensAfter?: number;
    details?: unknown;
  };
};

export type IngestResult = { ingested: boolean };
export type IngestBatchResult = { ingestedCount: number };

export type AssembleParams = {
  sessionId: string;
  sessionKey?: string;
  messages: unknown[];
  tokenBudget?: number;
};

export type CompactParams = {
  sessionId: string;
  sessionKey?: string;
  sessionFile: string;
  tokenBudget?: number;
  force?: boolean;
  currentTokenCount?: number;
  compactionTarget?: "budget" | "threshold";
  customInstructions?: string;
  runtimeContext?: Record<string, unknown>;
};

export type IngestParams = {
  sessionId: string;
  sessionKey?: string;
  message: unknown;
  isHeartbeat?: boolean;
};

export type IngestBatchParams = {
  sessionId: string;
  sessionKey?: string;
  messages: unknown[];
  isHeartbeat?: boolean;
};

export type AfterTurnParams = {
  sessionId: string;
  sessionKey?: string;
  sessionFile: string;
  messages: unknown[];
  prePromptMessageCount: number;
  autoCompactionSummary?: string;
  isHeartbeat?: boolean;
  tokenBudget?: number;
  runtimeContext?: Record<string, unknown>;
};

// ---------------------------------------------------------------------------
// Bridge implementation
// ---------------------------------------------------------------------------

export class ContextHubBridge {
  private sidecarUrl: string;

  constructor(sidecarUrl: string) {
    this.sidecarUrl = sidecarUrl.replace(/\/$/, "");
  }

  get info(): ContextEngineInfo {
    return {
      id: "contexthub",
      name: "ContextHub",
      ownsCompaction: false,
    };
  }

  async ingest(params: IngestParams): Promise<IngestResult> {
    return this.post("/ingest", params);
  }

  async ingestBatch(params: IngestBatchParams): Promise<IngestBatchResult> {
    const result = await this.post("/ingest-batch", params);
    return { ingestedCount: result.ingestedCount ?? 0 };
  }

  async assemble(params: AssembleParams): Promise<AssembleResult> {
    return this.post("/assemble", params);
  }

  async afterTurn(params: AfterTurnParams): Promise<void> {
    await this.post("/after-turn", params);
  }

  /**
   * ContextHub does not own compaction. Delegate to OpenClaw's built-in
   * runtime compaction path via `delegateCompactionToRuntime`.
   */
  async compact(params: CompactParams): Promise<CompactResult> {
    try {
      const sdk: { delegateCompactionToRuntime: (p: CompactParams) => Promise<CompactResult> } =
        await import("openclaw/plugin-sdk");
      return await sdk.delegateCompactionToRuntime(params);
    } catch {
      return { ok: false, compacted: false, reason: "runtime delegation unavailable" };
    }
  }

  async dispose(): Promise<void> {
    try {
      await this.post("/dispose", {});
    } catch {
      // Best-effort cleanup — sidecar may already be down.
    }
  }

  /** Forward a ContextHub tool call to the sidecar. */
  async dispatchTool(name: string, args: Record<string, unknown>): Promise<unknown> {
    return this.post("/dispatch", { name, args });
  }

  // -- HTTP helpers ----------------------------------------------------------

  private async post(path: string, body: unknown): Promise<any> {
    const resp = await fetch(`${this.sidecarUrl}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => "");
      throw new Error(`Sidecar POST ${path} failed: ${resp.status} ${text}`);
    }
    return resp.json();
  }
}
