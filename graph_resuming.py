from typing import Any, Callable, Dict, List, Set

import torch
import inspect
from collections import deque
from transformers import AutoModel
from torch.fx import GraphModule, Graph, Node
from transformers.modeling_outputs import BaseModelOutputWithPast


class Model(torch.nn.Module):
    def __init__(self, vocab_size=4096, d_model=128, n_heads=1, d_ff=256, dropout=0.1):
        super(Model, self).__init__()
        
        self.d_model = d_model
        self.n_heads = n_heads
        self.head_dim = d_model // n_heads
        assert d_model % n_heads == 0, "d_model must be divisible by n_heads"
        
        # Embedding layer
        self.embedding = torch.nn.Embedding(vocab_size, d_model)
        
        # Linear transformations for queries, keys, and values
        self.query_linear = torch.nn.Linear(d_model, d_model)
        self.key_linear = torch.nn.Linear(d_model, d_model)
        self.value_linear = torch.nn.Linear(d_model, d_model)
        
        # Output linear layer to combine heads
        self.out_linear = torch.nn.Linear(d_model, d_model)
        
        # Position-wise feed-forward network
        self.feed_forward = torch.nn.Sequential(
            torch.nn.Linear(d_model, d_ff),
            torch.nn.ReLU(),
            torch.nn.Linear(d_ff, d_model)
        )
        
        # Layer normalization layers
        self.norm1 = torch.nn.LayerNorm(d_model)
        self.norm2 = torch.nn.LayerNorm(d_model)
        
        # Dropout layer
        self.dropout = torch.nn.Dropout(dropout)

    def scaled_dot_product_attention(self, query, key, value):
        # Calculate attention scores
        scores = torch.matmul(query, key.transpose(-2, -1)) / (self.head_dim ** 0.5)
        attn_weights = torch.functional.F.softmax(scores, dim=-1)
        output = torch.matmul(attn_weights, value)
        return output

    def forward(self, input_ids):
        # Apply embedding layer
        x = self.embedding(input_ids)  # (batch_size, seq_length, d_model)
        
        batch_size, seq_length, _ = x.size()
        
        # Linear projections
        Q = self.query_linear(x)  # (batch_size, seq_length, d_model)
        K = self.key_linear(x)    # (batch_size, seq_length, d_model)
        V = self.value_linear(x)  # (batch_size, seq_length, d_model)
        
        # Split Q, K, V into multiple heads
        Q = Q.view(batch_size, seq_length, self.n_heads, self.head_dim).transpose(1, 2)  # (batch_size, n_heads, seq_length, head_dim)
        K = K.view(batch_size, seq_length, self.n_heads, self.head_dim).transpose(1, 2)  # (batch_size, n_heads, seq_length, head_dim)
        V = V.view(batch_size, seq_length, self.n_heads, self.head_dim).transpose(1, 2)  # (batch_size, n_heads, seq_length, head_dim)
        
        # Scaled dot-product attention
        attn_output = self.scaled_dot_product_attention(Q, K, V)  # (batch_size, n_heads, seq_length, head_dim)
        
        # Concatenate heads
        attn_output = attn_output.transpose(1, 2).contiguous().view(batch_size, seq_length, self.d_model)
        
        # Apply final linear transformation
        attn_output = self.out_linear(attn_output)
        
        # Add & Norm
        x = x + self.dropout(attn_output)
        x = self.norm1(x)
        
        # Feed-forward block
        ff_output = self.feed_forward(x)
        x = x + self.dropout(ff_output)
        x = self.norm2(x)
        
        return BaseModelOutputWithPast(last_hidden_state=x)
    

def get_target_nodes(graph: GraphModule, targets: List[str]):
    target_nodes = []
    for node in graph.graph.nodes:
        if (
            node.op == "call_module" and
            type(graph.get_submodule(node.target)).__name__ in targets
        ):
            target_nodes.append(node)

    return target_nodes


def check_assumption(graph: Graph) -> bool:
    for node in graph.nodes:
        for user in node.users:
            if node not in user.all_input_nodes:
                return False

        for input_node in node.all_input_nodes:
            if node not in input_node.users:
                return False

        if (
            len(node.users) != len(set(node.users)) or 
            len(node.all_input_nodes) != len(set(node.all_input_nodes))
        ):
            return False
        
    return True


def topological_partition(graph: GraphModule, target_nodes: Set[Node]) -> List[List[Node]]:
    # use list representation to maintain topological sorting
    assert check_assumption(graph.graph)

    partitions: List[List[Node]] = [[]]
    remaining_indegrees = {node: len(node.all_input_nodes) for node in graph.graph.nodes}
    partition_index = 0  # global counter, not necessary but ensures partitions are connected

    # start with graph input nodes
    queue = deque(node for node in graph.graph.nodes if remaining_indegrees[node] == 0)
    while len(queue) > 0:
        node = queue.popleft()

        # guarantee targets are assigned to disjoint partitions
        if node in target_nodes:
            partition_index += 1
            partitions.append([])

        # assign to partition
        partitions[partition_index].append(node)

        # recurse on last indegree only in order to guarantee that
        # the node is assigned to maximal partition
        for user in node.users:
            remaining_indegrees[user] -= 1
            if remaining_indegrees[user] == 0:
                queue.append(user)

    assert set().union(*partitions) == set(graph.graph.nodes)
    return partitions


def partition_graph(model: torch.nn.Module, partitions: List[List[Node]]):
    subgraphs = []

    # create subgraphs
    for partition_nodes in partitions:
        # create a new graph for the partition
        subgraph = Graph(model)
        node_map = {}

        # add placeholders for inputs not in this subgraph. use set to deduplicate
        new_input_nodes = {
            input_node
            for node in partition_nodes
            for input_node in node.all_input_nodes
            if input_node not in partition_nodes
        }
        for input_node in new_input_nodes:
            node_map[input_node] = subgraph.placeholder(input_node.name)

        # add the nodes to subgraph
        for node in partition_nodes:
            node_map[node] = subgraph.node_copy(node, lambda n: node_map[n])

        # add an output node to collect all subgraph outputs into a dictionary
        if len(subgraph.find_nodes(op="output")) <= 0:
            output_dict = {
                node.name: node_map[node]
                for node in partition_nodes
                if any(user not in partition_nodes for user in node.users.keys())
            }
            subgraph.output(output_dict)

        # Save the subgraph for this partition
        subgraph.lint()
        input_names = [node.name for node in subgraph.nodes if node.op == "placeholder"]
        subgraphs.append({
            "code": subgraph.python_code("self"),
            "input_names": input_names,
            "consumed_names": [],
        })

        print([n for n in subgraph.nodes])
        assert check_assumption(subgraph)

    # populate consumed_names according to when inputs are last used
    # in order to vacate the `intermediates` cache and save memory
    all_input_names = set().union(*(subgraph["input_names"] for subgraph in subgraphs))
    for input_name in all_input_names:
        for subgraph in reversed(subgraphs):
            if input_name in subgraph["input_names"]:
                subgraph["consumed_names"].append(input_name)
                break
        else:
            assert False
    
    return subgraphs


def gptq_compress(module: torch.nn.Module, inputs: List[torch.Tensor]):
    print("gptq_compress")
    pass


class HookedModel:
    def __init__(self):
        self.hook_targets = []
        self.graph = None
        self.subgraphs = []
        self.model = None

    def register_hook(self, func: Callable, targets: List[str]):
        self.hook_targets.append((func, targets))

    def init_forward(self, model: torch.nn.Module):
        self.model = model

        # 1. create graph
        # TODO: better tracing of submodules/nn.sequential, although I don't think the
        # current implementation covers this case either
        self.graph: GraphModule = symbolic_trace(model)

        # 2. identify target nodes
        target_nodes = set().union(*(
            get_target_nodes(self.graph, targets)
            for func, targets in self.hook_targets
        ))

        # 3. cut into partitions along target nodes
        partitions: List[List[Node]] = topological_partition(self.graph, target_nodes)
        self.subgraphs: List[GraphModule] = partition_graph(model, partitions)
    
    def forward(self, *args, **kwargs):
        # 4. perform compression
        intermediates = kwargs.copy()
        for subgraph_index, subgraph in enumerate(self.subgraphs):
            code = subgraph["code"]
            exec(code.src, code.globals)
            forward_function = code.globals.get("forward")

            inputs = {input_name: intermediates[input_name] for input_name in subgraph["input_names"]}

            # TODO: detect and call hooks
            

            if subgraph_index < len(self.subgraphs) - 1:
                intermediates.update(forward_function(self.model, **inputs))

                for consumed_name in subgraph["consumed_names"]:
                    del intermediates[consumed_name]
            else:
                return forward_function(self.model, **inputs)


if __name__ == "__main__":
    use_dummy_model = True
    sequence_length = 2048

    if use_dummy_model:
        model = Model()
        from torch.fx import symbolic_trace
    else:
        model = AutoModel.from_pretrained("meta-llama/Llama-3.2-1B-Instruct")
        from transformers.utils.fx import symbolic_trace

    data_loader = [
        {"input_ids": torch.zeros(sequence_length, dtype=torch.int32).reshape(1, sequence_length)},
        {"input_ids": torch.zeros(sequence_length, dtype=torch.int32).reshape(1, sequence_length)},
        {"input_ids": torch.zeros(sequence_length, dtype=torch.int32).reshape(1, sequence_length)},
    ]

    # modifier inits
    hooked_model = HookedModel()
    hooked_model.register_hook(gptq_compress, ["Linear"])

    # some time after modifier inits but before forward passes
    hooked_model.init_forward(model)

    # oneshot/ eval loop
    model.eval()
    with torch.no_grad():
        for batch in data_loader:
            hooked_output = hooked_model.forward(**batch)
            model_output = model.forward(**batch)
            assert torch.equal(hooked_output["last_hidden_state"], model_output["last_hidden_state"])