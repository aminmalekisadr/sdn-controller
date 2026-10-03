import os
import net_sim_builder as netsim
import networkx as nx
from PHY_model import link_delay_ns, node_forwarding_delay_ns

"""
In this topology design, the switching capability of the NPUs 
is modeled as separate switches connected to the compute chips 
at very high speeds which the most accurate way of modelling

The david indices within the NPU are arranged along the 
x-axis numbers, meaning davids within one board will have
adjacent indices.

Also the switches indices belonging to the same rack but in 
different planes are adjacent. So it's like this: 
[plane0_indices:plane1_indices:plane2_indices:plane3_indices]

ID layout (all contiguous, auto-derived):
  Computes (david dies):  0 .. TOTAL_COMPUTES-1
  David switches: GRID_START .. GRID_START+TOTAL_COMPUTES-1
  Leaf/Spine:     SW_BASE .. SW_BASE + NUM_RACKS*PLANES*16 - 1
  HRS switches:     HRS_BASE .. HRS_BASE + 255          (exactly 256 IDs)
"""

"""
Two planes are removed to open up ports for the SURE connections
"""

# ---------------- Params ----------------
NUM_RACKS               = 2
BOARDS_PER_RACK         = 8
DAVIDS_PER_BOARD        = 8
IO_PER_COMPUTE          = 2 # don't go beyond 2
NUM_PLANES              = 4 # These are the back-plane switches as explained in sec. 3.3 of the UB-Mesh paper
L1_UNIONS_PER_PLANE     = 16
EXT_UNIONS_PER_PLANE    = 8 # edges for each EXT = 8 // EXT_UNIONS_PER_PLANE
HRS_PER_PLANE           = 8  # edges for each HRS = 8 // HRS_PER_PLANE
NUM_SWITCH_DOMAINS      = 1
NUM_SURE_OCS            = 1
SURE_MIRROR_PER_OCS     = 128
SURE_EPS_PER_RACK       = 0
NUM_SURE_BACKUP_OCS     = 0
SURE_MIRROR_PER_BACKUP_OCS = 0
NUM_RACKS_PER_SWITCH_DOMAIN = 8
# ----------------------------------------

SPEED_LAT = {
    "io_switch":        ('10000Gbps', '0ns'), # adding this to avoid host-multi switch routing issue
    "compute_to_grid":  ('5600Gbps', '1ns'),
    "compute_to_leaf":  ('400Gbps', f"{int(link_delay_ns('passive_copper', 2))}ns"),
    "d2d":              ('400Gbps', f"{int(link_delay_ns('passive_copper', 0))}ns"),
    "leaf_to_ext":      ('400Gbps', f"{int(link_delay_ns('passive_copper', 2))}ns"),
        # According to the UB-Mesh paper, these interconnects are active 
        # electrical interconnects which incur a latency of 100 - 200ns due to 
        # CDR (Clock and Data Recovery) mechanisms. Calc breakdowns in the function
    "spine_to_hrs":     ('400Gbps', f"{int(link_delay_ns('active_copper', 10))}ns"),
        # Even though the propagation delay of fiber is very low,
        # the processing required for the SerDes conversion is a fixed
        # 300ns delay
    "optical":     ('400Gbps', f"{int(link_delay_ns('optical', 10, 'lpo'))}ns")
}

# --------- Derived sizes & ID bases ---------
TOTAL_COMPUTES      = NUM_RACKS * BOARDS_PER_RACK * DAVIDS_PER_BOARD
COMPUTE_PER_RACK    = BOARDS_PER_RACK * DAVIDS_PER_BOARD
SW_PER_PLANE        = L1_UNIONS_PER_PLANE + EXT_UNIONS_PER_PLANE
SWITCHES_PER_RACK   = NUM_PLANES * SW_PER_PLANE              # 64 (L1) + 16 (EXT) per rack
TOTAL_BOARDS          = NUM_RACKS * BOARDS_PER_RACK

# GRID_SHIFT      = TOTAL_COMPUTES if IO_PER_COMPUTE > 1 else 0 # adding this to avoid host-multi switch routing issue
GRID_SHIFT      = 0 
GRID_START      = TOTAL_COMPUTES + GRID_SHIFT # On-chip IO is modeled as separate from the compute w/ very high-speed connectivity 
SW_BASE         = GRID_START + TOTAL_COMPUTES * IO_PER_COMPUTE
HRS_START       = SW_BASE + NUM_RACKS * SWITCHES_PER_RACK    # starts right after leaf/spine
NUM_HRS         = NUM_SWITCH_DOMAINS * NUM_PLANES * HRS_PER_PLANE
#NUM_HRS         = ((NUM_RACKS-1)//BOARDS_PER_RACK)*HRS_PER_RACK + HRS_PER_RACK # per swicthing domain (8 racks)
#OXC_START       = HRS_START + NUM_HRS
#NUM_OXC         = NUM_RACKS
SURE_EPS_START  = HRS_START + NUM_HRS
NUM_SURE_EPS    = SURE_EPS_PER_RACK * NUM_RACKS
SURE_OCS_START       = SURE_EPS_START + NUM_SURE_EPS
NUM_SURE_OCS_MIRRORS = NUM_SURE_OCS * SURE_MIRROR_PER_OCS
SURE_BACKUP_OCS_START = SURE_OCS_START + NUM_SURE_OCS_MIRRORS


# --------------------------------------------
# -------- For plane-aware routing -----------
intra_rack_switches = set(range(SW_BASE, HRS_START))
host_ids = set(range(TOTAL_COMPUTES)) 
hrs_switches = set(range(HRS_START, HRS_START + NUM_HRS))
io_switches = set(range(GRID_START, SW_BASE))
inside = intra_rack_switches | host_ids | io_switches
outside = hrs_switches
# ------------------------------------------------

def rack_of_david(d): return d // COMPUTE_PER_RACK

def npu_idx_of_david_within_rack(d): return (d % COMPUTE_PER_RACK) // (DAVIDS_PER_BOARD)


def all_simple_paths(G, source, target):
    try:
        # 这里你可以在networkx库中寻找适合的寻路函数。
        # 调用networkx库的all_simple_paths函数，可以获得跳数<=cutoff值的所有不成环路径。
        paths = nx.all_simple_paths(G, source, target, cutoff=2)
    except nx.NetworkXNoPath:
        paths = []
    return paths

def all_shortest_paths(G, source, target):
    try:
        # 这里你可以在networkx库中寻找适合的寻路函数。
        # 调用networkx库的all_shortest_path函数，可以获得所有最短路径。
        paths = nx.all_shortest_paths(G, source, target)
    except nx.NetworkXNoPath:
        paths = []
    return paths

def scaleup_paths(graph, source, dest):
    allowed_nodes = inside
    '''
    same_rack = rack_of_david(source) == rack_of_david(dest)
    if not same_rack:
        # We are forcing inter-rack traffic in SURE to go through the 
        # intra-rack scale up connections and then through the opticals
        allowed_nodes = inside
    else:
        allowed_nodes = inside | outside
    '''
    sub = graph.subgraph(allowed_nodes)
    return nx.all_shortest_paths(sub, source, dest)



if __name__ == '__main__':
    graph = netsim.NetworkSimulationGraph()

    leaf_ids = []
    spine_ids = []
    # step1: Add the davids
    for compute_id in range(TOTAL_COMPUTES):
        graph.add_netisim_host(compute_id, forward_delay='1ns')

    # step1.1: add io routers (to avoid routing issue with host-mult switches)
    for io_sw_id in range(TOTAL_COMPUTES, TOTAL_COMPUTES + GRID_SHIFT):
        graph.add_netisim_node(io_sw_id, forward_delay='0ns')

    # step2: Add the grid switches (on-chip IO/controllers)
    for grid_id in range(GRID_START, SW_BASE):
        graph.add_netisim_node(grid_id, forward_delay=f"{int(node_forwarding_delay_ns('low_latency_UB'))}ns") # Data came from Haitao in one of our meetings

    # step3: Add the SFU switches (normal + extension unions)
    for sw_id in range(SW_BASE, SW_BASE + NUM_RACKS * SWITCHES_PER_RACK):
        graph.add_netisim_node(sw_id, forward_delay=f"{int(node_forwarding_delay_ns('low_latency_UB'))}ns") # Data came from Haitao in one of our meetings

    # step4: Add the 5808 HRS switches
    for sw_id in range(HRS_START, HRS_START + NUM_HRS):
        graph.add_netisim_node(sw_id, forward_delay=f"{int(node_forwarding_delay_ns('broadcom_tomahawk'))}ns") # 30ns due to the switch SerDes

    for sw_id in range(SURE_EPS_START, SURE_EPS_START + NUM_SURE_EPS):
        graph.add_netisim_node(sw_id, forward_delay=f"{int(node_forwarding_delay_ns('low_latency_UB'))}ns")

    # Step6: The NPU connections to on-chip IO controller
    for compute_id in range(TOTAL_COMPUTES, TOTAL_COMPUTES + GRID_SHIFT):
        graph.add_netisim_edge(compute_id - TOTAL_COMPUTES, compute_id - TOTAL_COMPUTES + GRID_SHIFT, bandwidth=SPEED_LAT['io_switch'][0], delay=SPEED_LAT['io_switch'][1], edge_count=1)

    for compute_id in range(TOTAL_COMPUTES):
        for iodie_id in range(IO_PER_COMPUTE):
            graph.add_netisim_edge(compute_id + GRID_SHIFT, compute_id + GRID_START + (TOTAL_COMPUTES*iodie_id), bandwidth=SPEED_LAT['compute_to_grid'][0], delay=SPEED_LAT['compute_to_grid'][1], edge_count=1)

    # step7: intra-BOARD d2d connections
    for board_idx in range(TOTAL_BOARDS):
        npu_start_idx = board_idx * DAVIDS_PER_BOARD
        npu_end_idx = npu_start_idx + DAVIDS_PER_BOARD
        io_idx = TOTAL_COMPUTES if IO_PER_COMPUTE > 1 else 0
        for i in range(npu_start_idx, npu_end_idx):
            for j in range(i+1, npu_end_idx):
                # Always connect board A
                graph.add_netisim_edge(i+GRID_START, j+GRID_START, bandwidth=SPEED_LAT['d2d'][0], delay=SPEED_LAT['d2d'][1], edge_count=1)
  
    # step8: NPU to L1 flat union layer
    for n in range(TOTAL_BOARDS):
        davids = list(range(n*DAVIDS_PER_BOARD, (n+1)* DAVIDS_PER_BOARD))
        # ios = [d + GRID_START for d in davids]
        io_groups = []
        for iodie_id in range(IO_PER_COMPUTE):
            io_group = [d + GRID_START + (TOTAL_COMPUTES*iodie_id) for d in davids]
            io_groups.append(io_group)

        ios_A = io_groups[0] # 2 out of frame ports for D1-D4, 6 out of frame ports for D5-D8
        if len(io_groups) == 2:
            ios_B = io_groups[1] # 6 out of frame ports ... then 2
        else:
            ios_B = ios_A

        rack_idx = n // BOARDS_PER_RACK
        rack_sw_base = SW_BASE + rack_idx * SWITCHES_PER_RACK
        for p in range(NUM_PLANES):
            l1_union_base = p * SW_PER_PLANE + rack_sw_base
            npu_idx_within_rack = (n % BOARDS_PER_RACK)
            left_plane_union = 2*npu_idx_within_rack + l1_union_base
            right_plane_union = 2*npu_idx_within_rack +1 + l1_union_base
            # The iodieA is the detour die. The out-of-rack switch connecting to iodieA will be the detour switch for that NPU
            # We want different NPUs in a board to have different detour switches so we don't have one single point of failure. 
            # That's why the iodieA is connected to the L1 switches in a round-robin fashion
            for io_idx in range(len(ios_A)): 
                if p == io_idx % NUM_PLANES:
                    # Contribute both ports to optical
                    # graph.add_netisim_edge(ios_A[io_idx], left_plane_union, bandwidth=SPEED_LAT['compute_to_leaf'][0], delay=SPEED_LAT['compute_to_leaf'][1], edge_count=1)
                    # graph.add_netisim_edge(ios_A[io_idx], right_plane_union, bandwidth=SPEED_LAT['compute_to_leaf'][0], delay=SPEED_LAT['compute_to_leaf'][1], edge_count=1)
                    pass
                else:
                    # io_idx +1 is chosen arbitrarily. We just need one b die to contribute a port for optical
                    if p == (io_idx +1) % NUM_PLANES:
                        graph.add_netisim_edge(ios_B[io_idx], left_plane_union, bandwidth=SPEED_LAT['compute_to_leaf'][0], delay=SPEED_LAT['compute_to_leaf'][1], edge_count=1)
                        graph.add_netisim_edge(ios_B[io_idx], right_plane_union, bandwidth=SPEED_LAT['compute_to_leaf'][0], delay=SPEED_LAT['compute_to_leaf'][1], edge_count=1)
                    else:
                        graph.add_netisim_edge(ios_B[io_idx], left_plane_union, bandwidth=SPEED_LAT['compute_to_leaf'][0], delay=SPEED_LAT['compute_to_leaf'][1], edge_count=1)
                        graph.add_netisim_edge(ios_B[io_idx], right_plane_union, bandwidth=SPEED_LAT['compute_to_leaf'][0], delay=SPEED_LAT['compute_to_leaf'][1], edge_count=1)

    # step9: L1 unions <--> extension unions
    for rack in range(NUM_RACKS):
        rack_sw_base = SW_BASE + rack * SWITCHES_PER_RACK
        for plane in range(NUM_PLANES):
            plane_sw_base = rack_sw_base + plane * SW_PER_PLANE
            for u_pair in range(L1_UNIONS_PER_PLANE//2):
                left_plane_union = u_pair*2 + plane_sw_base
                right_plane_union = u_pair*2 + 1 + plane_sw_base
                for ext_pair in range(EXT_UNIONS_PER_PLANE//2):
                    left_plane_ext = ext_pair + plane_sw_base + L1_UNIONS_PER_PLANE
                    right_plane_ext = ext_pair + EXT_UNIONS_PER_PLANE//2 + plane_sw_base + L1_UNIONS_PER_PLANE
                    graph.add_netisim_edge(left_plane_union, left_plane_ext, bandwidth=SPEED_LAT['leaf_to_ext'][0], delay=SPEED_LAT['leaf_to_ext'][1], edge_count=1)
                    graph.add_netisim_edge(right_plane_union, right_plane_ext, bandwidth=SPEED_LAT['leaf_to_ext'][0], delay=SPEED_LAT['leaf_to_ext'][1], edge_count=1)

    # step10: L1 unions <--> 5808 HRS
    for rack in range(NUM_RACKS):
        rack_sw_base = SW_BASE + rack * SWITCHES_PER_RACK
        hrs_sw_base = HRS_START + (rack // NUM_RACKS_PER_SWITCH_DOMAIN) * NUM_PLANES * HRS_PER_PLANE
        for plane in range(NUM_PLANES):
            plane_sw_base = rack_sw_base + plane * SW_PER_PLANE
            plane_hrs_base =  hrs_sw_base + plane * HRS_PER_PLANE
            for u_pair in range(L1_UNIONS_PER_PLANE//2):
                left_plane_union = u_pair*2 + plane_sw_base
                right_plane_union = u_pair*2 + 1 + plane_sw_base
                for hrs_pair in range(HRS_PER_PLANE//2):
                    left_plane_hrs = hrs_pair + plane_hrs_base
                    right_plane_hrs = hrs_pair + HRS_PER_PLANE//2 + plane_hrs_base
                    graph.add_netisim_edge(left_plane_union, left_plane_hrs, bandwidth=SPEED_LAT['spine_to_hrs'][0], delay=SPEED_LAT['spine_to_hrs'][1], edge_count=1)
                    graph.add_netisim_edge(right_plane_union, right_plane_hrs, bandwidth=SPEED_LAT['spine_to_hrs'][0], delay=SPEED_LAT['spine_to_hrs'][1], edge_count=1)

    # step11: SURE OCS Connections
    # For the 2 hop scenario, the first port that each NPU is contributing is just like the normal one and it's 
    # direct pairwise. But the second port they contribute is inter-board
    for david in range(COMPUTE_PER_RACK):
        rack1_david_dieA = david + GRID_START
        rack1_david_board_idx = rack1_david_dieA % DAVIDS_PER_BOARD
        rack1_david_board_id = david // DAVIDS_PER_BOARD
        rack2_david_board_id = rack1_david_board_idx
        rack2_david_board_idx = rack1_david_board_id
        rack2_david_dieA = GRID_START + COMPUTE_PER_RACK + rack2_david_board_id * DAVIDS_PER_BOARD + rack2_david_board_idx
        graph.add_netisim_edge(rack1_david_dieA, rack2_david_dieA, bandwidth=SPEED_LAT['optical'][0], delay=SPEED_LAT['optical'][1], edge_count=1)
        graph.add_netisim_edge(rack1_david_dieA, rack2_david_dieA, bandwidth=SPEED_LAT['optical'][0], delay=SPEED_LAT['optical'][1], edge_count=1)


    # step3: 生成配置文件,build_graph_config会生成一系列中间数据,最终生成dcn2.0_config.xml文件
    graph.build_graph_config()
    # step3.2: gen_route_table 寻路并生成路由表
    graph.gen_route_table(path_finding_algo=scaleup_paths, multiple_workers=4)
    #graph.gen_route_table(path_finding_algo=all_shortest_paths, multiple_workers=4)
    # step3.3: 配置 TP Channel，当前TP Channel的配置策略是基于路由表项，每一条表项对应一个路径，每个路径对应多个优先级
    graph.config_transport_channel(priority_list = [7])
    # step3.4: 写入所有配置文件
    graph.write_config()  
