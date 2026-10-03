"""Stub of net_sim_builder: records nodes and edges in creation order; everything else is a no-op."""

LAST = None  # the most recently created graph, read by the runner


class NetworkSimulationGraph:
    def __init__(self):
        global LAST
        self.hosts = []      # compute ids, in creation order
        self.nodes = []      # (id, forward_delay) for switch-like nodes
        self.edges = []      # (a, b, bandwidth, delay), one per edge_count, in creation order
        self.output_dir = ""
        LAST = self

    def add_netisim_host(self, node_id, forward_delay=None):
        self.hosts.append(node_id)

    def add_netisim_node(self, node_id, forward_delay=None):
        self.nodes.append((node_id, forward_delay))

    def add_netisim_edge(self, a, b, bandwidth=None, delay=None, edge_count=1):
        for _ in range(edge_count):
            self.edges.append((a, b, bandwidth, delay))

    def build_graph_config(self, *args, **kwargs):
        pass

    def gen_route_table(self, *args, **kwargs):
        pass

    def config_transport_channel(self, *args, **kwargs):
        pass

    def write_config(self, *args, **kwargs):
        pass
