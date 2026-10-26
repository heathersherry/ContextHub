"""Shared index options for database initialization and experiment runs."""

from .schemas import IndexConfig


def add_embedding_index_arguments(parser):
    parser.add_argument("--embedding-model", help="Override index.embedding_model in the config")
    parser.add_argument("--embedding-dimensions", type=int, help="Override index.dimensions; must match the research database")


def resolve_index_config(config, args):
    values = dict(config["index"])
    if args.embedding_model is not None:
        values["embedding_model"] = args.embedding_model
    if args.embedding_dimensions is not None:
        values["dimensions"] = args.embedding_dimensions
    return IndexConfig.model_validate(values)

