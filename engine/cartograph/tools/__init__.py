"""The graph tool functions behind the ``carto`` query commands.

Each returns a plain dict; ``cli.py`` wraps it in the capability envelope.
"""

from __future__ import annotations

# Re-export names that external code may patch via "cartograph.tools.*"
from ..changes import parse_diff_ranges as parse_diff_ranges
from ..changes import parse_git_diff_ranges as parse_git_diff_ranges
from ..changes import parse_svn_diff_ranges as parse_svn_diff_ranges
from ..incremental import (
    get_changed_files as get_changed_files,
)
from ..incremental import (
    get_staged_and_unstaged as get_staged_and_unstaged,
)

# -- _common ----------------------------------------------------------------
from ._common import (
    _BUILTIN_CALL_NAMES,
    _get_store,
    _validate_repo_root,
)

# -- build ------------------------------------------------------------------
from .build import build_or_update_graph, run_postprocess

# -- community_tools --------------------------------------------------------
from .community_tools import (
    get_architecture_overview_func,
    get_community_func,
    list_communities_func,
)

# -- context ----------------------------------------------------------------
from .context import get_minimal_context

# -- docs -------------------------------------------------------------------
from .docs import embed_graph

# -- flows_tools ------------------------------------------------------------
from .flows_tools import get_flow, list_flows

# -- query ------------------------------------------------------------------
from .query import (
    find_large_functions,
    get_impact_radius,
    list_graph_stats,
    query_graph,
    semantic_search_nodes,
)

# -- refactor_tools ---------------------------------------------------------
from .refactor_tools import refactor_func

# -- review -----------------------------------------------------------------
from .review import (
    detect_changes_func,
    get_review_context,
)

__all__ = [
    # _common
    "_BUILTIN_CALL_NAMES",
    "_get_store",
    "_validate_repo_root",
    # build
    "build_or_update_graph",
    "run_postprocess",
    # context
    "get_minimal_context",
    # community_tools
    "get_architecture_overview_func",
    "get_community_func",
    "list_communities_func",
    # docs
    "embed_graph",
    # flows_tools
    "get_flow",
    "list_flows",
    # query
    "find_large_functions",
    "get_impact_radius",
    "list_graph_stats",
    "query_graph",
    "semantic_search_nodes",
    # refactor_tools
    "refactor_func",
    # review
    "detect_changes_func",
    "get_review_context",
    # re-exported for backward compat (used in test patches)
    "get_changed_files",
    "get_staged_and_unstaged",
    "parse_git_diff_ranges",
    "parse_svn_diff_ranges",
    "parse_diff_ranges",
]
