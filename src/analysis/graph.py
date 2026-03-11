import networkx as nx

from config.settings import CLUSTER_MIN_SIZE, CEX_FUNDER_THRESHOLD

# Global directed graph — persists for the lifetime of the process
_graph: nx.DiGraph = nx.DiGraph()


def add_funder_edges(wallet: str, funders: list[str]) -> None:
    """Add edges funder → wallet for each funder found."""
    _graph.add_node(wallet.lower())  # ensure node exists even if no funders
    for funder in funders:
        _graph.add_edge(funder.lower(), wallet.lower())


def _is_cex(funder: str) -> bool:
    """Return True if this funder looks like a CEX/bridge (too many successors)."""
    return _graph.out_degree(funder) > CEX_FUNDER_THRESHOLD


def get_cluster(wallet: str) -> list[str]:
    """
    Return all suspect wallets that share at least one non-CEX funder with the given wallet.
    A cluster is: all successors of a common funder node.
    """
    wallet = wallet.lower()
    cluster = set()

    for funder in _graph.predecessors(wallet):
        if _is_cex(funder):
            continue
        siblings = list(_graph.successors(funder))
        if len(siblings) >= CLUSTER_MIN_SIZE:
            cluster.update(siblings)

    cluster.discard(wallet)
    return list(cluster)


def is_cluster_alert(wallet: str) -> bool:
    """Return True if wallet belongs to a cluster large enough to trigger an alert."""
    wallet = wallet.lower()

    for funder in _graph.predecessors(wallet):
        if _is_cex(funder):
            continue
        if _graph.out_degree(funder) >= CLUSTER_MIN_SIZE:
            return True

    return False
