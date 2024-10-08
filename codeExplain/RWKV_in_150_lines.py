########################################################################################################
# The RWKV Language Model - https://github.com/BlinkDL/RWKV-LM
########################################################################################################

import numpy as np
# precision=4：设置浮点数的打印精度为 4 位小数。suppress=True：在打印小数时，避免使用科学计数法（例如，1e-10 将被打印为 0.0000）。linewidth=200：设置每行的最大字符数为 200，超出部分将换行。
np.set_printoptions(precision=4, suppress=True, linewidth=200)
import types, torch
from torch.nn import functional as F
# huggingface提供的一个分词器，可以将文本转换为模型可以理解的token
from tokenizers import Tokenizer

# 从文件中加载分词器
tokenizer = Tokenizer.from_file("./resource/20B_tokenizer.json")

# args = types.SimpleNamespace() 用于创建一个简单的命名空间对象，允许你以点（.）的方式访问属性。这通常用于存储参数或配置。
args = types.SimpleNamespace()
args.MODEL_NAME = './resource/RWKV-4-Pile-430M-20220808-8066'
args.n_layer = 24
args.n_embd = 1024

context = "\nIn a shocking finding, scientist discovered a herd of dragons living in a remote, previously unexplored valley, in Tibet. Even more surprising to the researchers was the fact that the dragons spoke perfect Chinese."
NUM_TRIALS = 3  # 尝试生成文本的次数。
LENGTH_PER_TRIAL = 100  # 每次尝试生成的文本长度。
TEMPERATURE = 1.0  # 控制生成文本的随机性的参数。值越大，生成的文本越随机；值越小，生成的文本越确定。
TOP_P = 0.85 # 在生成文本时，只考虑累积概率超过此值的词汇。

########################################################################################################

# torch.jit.ScriptModule 是 PyTorch 的一部分，用于创建可优化和序列化的模型。它通过将 Python 代码转换为 TorchScript 代码，允许在不依赖 Python 解释器的情况下运行模型，从而提高性能和可移植性。
class RWKV_RNN(torch.jit.ScriptModule):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.eval() # set torch to inference mode
        
        # 将模型加载到CPU上
        w = torch.load(args.MODEL_NAME + '.pth', map_location='cpu')
        for k in w.keys():
            # 使用 squeeze() 方法去掉单维度（即大小为 1 的维度），以确保权重的形状更为紧凑。
            if      '.time_' in k: w[k] = w[k].squeeze()
            # 对于键中包含 .time_decay 的权重，首先将其转换为浮点型，然后计算其负指数。
            if '.time_decay' in k: w[k] = -torch.exp(w[k].float()) # the real time decay is like e^{-e^x}
            else: w[k] = w[k].float() # convert to f32 type
        
        # 这个和w不一样
        self.w = types.SimpleNamespace() # set self.w from w
        self.w.blocks = {}
        # 将点分隔的键转换为嵌套的命名空间对象，以便更容易访问权重。
        for k in w.keys(): # example: "blocks.0.att.time_first" => self.w.blocks[0].att.time_first
            parts = k.split('.')
            last = parts.pop()
            here = self.w
            for p in parts:
                if p.isdigit():
                    p = int(p)
                    if p not in here: here[p] = types.SimpleNamespace()
                    here = here[p]
                else:
                    if not hasattr(here, p): setattr(here, p, types.SimpleNamespace())
                    here = getattr(here, p)
            setattr(here, last, w[k])   

    def layer_norm(self, x, w):
        # 第二个参数指定归一化的维度，后面两个是归一化中的两个参数
        return F.layer_norm(x, (self.args.n_embd,), weight=w.weight, bias=w.bias)

    @torch.jit.script_method
    def channel_mixing(self, x, state, i:int, time_mix_k, time_mix_r, kw, vw, rw):
        # x就是当前的token,state是上一个token
        xk = x * time_mix_k + state[5*i+0] * (1 - time_mix_k)
        xr = x * time_mix_r + state[5*i+0] * (1 - time_mix_r)
        state[5*i+0] = x
        r = torch.sigmoid(rw @ xr)
        k = torch.square(torch.relu(kw @ xk)) # square relu, primer paper
        return r * (vw @ k)

    @torch.jit.script_method
    def time_mixing(self, x, state, i:int, time_mix_k, time_mix_v, time_mix_r, time_first, time_decay, kw, vw, rw, ow):
        # time_mix_k.size(): [1024]
        # time_mix_v.size(): [1024]
        # time_mix_r.size(): [1024]
        # x.size(): [1024]
        # state.size(): [120, 1024]
        # kw.size(): [1024, 1024]
        # vw.size(): [1024, 1024]
        # rw.size(): [1024, 1024]
        # ow.size(): [1024, 1024]
        # xk.size(): [1024]
        # xv.size(): [1024]
        # xr.size(): [1024]
        # r.size(): [1024]
        # k.size(): [1024]
        # v.size(): [1024]
        # aa.size(): [1024]
        # bb.size(): [1024]
        # pp.size(): [1024]
        # ww.size(): [1024]
        # qq.size(): [1024]
        # e1.size(): [1024]
        # e2.size(): [1024]
        # a.size(): [1024]
        # b.size(): [1024]
        # wkv.size(): [1024]
        # ww.size(): [1024]
        # time_decay.size(): [1024]
        # qq.size(): [1024]
        # e1.size(): [1024]
        # e2.size(): [1024]
        # time_first: [1024]

        # time_first是时间衰减的初始值，time_decay是时间衰减率，kw、vw、rw、ow是权重矩阵
        # 因为i是从0开始的，所以才会有5*i+1,在初始的时候，访问的是1
        xk = x * time_mix_k + state[5*i+1] * (1 - time_mix_k)  
        xv = x * time_mix_v + state[5*i+1] * (1 - time_mix_v)
        xr = x * time_mix_r + state[5*i+1] * (1 - time_mix_r)
        state[5*i+1] = x
        # 得到3个1024维的向量
        r = torch.sigmoid(rw @ xr)
        k = kw @ xk
        v = vw @ xv
        
        # 这个命名真他妈垃圾
        aa = state[5*i+2]
        bb = state[5*i+3]
        # pp是上一步的时间衰减权重，ww是当前的时间衰减权重，qq是两者中较大的那个
        pp = state[5*i+4]
        # 初始的时间衰减权重加上k向量，相当于公式中的u
        ww = time_first + k
        # 逐元素进行比较，返回较大的元素
        qq = torch.maximum(pp, ww)
        # 下面减法这里是为了防止指数爆炸，这里是在计算指数的时候，减去一个较大的数，是的指数永远比1小或者等于
        e1 = torch.exp(pp - qq)
        e2 = torch.exp(ww - qq)
        a = e1 * aa + e2 * v
        b = e1 * bb + e2
        wkv = a / b
        ww = pp + time_decay
        qq = torch.maximum(ww, k)
        e1 = torch.exp(ww - qq)
        e2 = torch.exp(k - qq)
        # 使用state来存储累计的指数和
        state[5*i+2] = e1 * aa + e2 * v
        state[5*i+3] = e1 * bb + e2
        state[5*i+4] = qq
        return ow @ (r * wkv)

    def forward(self, token, state):
        with torch.no_grad():
            if state == None:
                # n_layer是模型总共的层数，*5是因为每个层需要存那么多的向量，并且将列维度设为n_embd
                state = torch.zeros(self.args.n_layer * 5, self.args.n_embd)
                # 确保每一层的第四个元素为负无穷，确保e的指数近0
                for i in range(self.args.n_layer): state[5*i+4] = -1e30 # -infinity
            
            # 将输入的token嵌入矩阵转化为嵌入向量
            x = self.w.emb.weight[token]
            x = self.layer_norm(x, self.w.blocks[0].ln0)
            for i in range(self.args.n_layer):
                att = self.w.blocks[i].att
                x = x + self.time_mixing(self.layer_norm(x, self.w.blocks[i].ln1), state, i, 
                    att.time_mix_k, att.time_mix_v, att.time_mix_r, att.time_first, att.time_decay, 
                    att.key.weight, att.value.weight, att.receptance.weight, att.output.weight)
                ffn = self.w.blocks[i].ffn
                x = x + self.channel_mixing(self.layer_norm(x, self.w.blocks[i].ln2), state, i, 
                    ffn.time_mix_k, ffn.time_mix_r, 
                    ffn.key.weight, ffn.value.weight, ffn.receptance.weight)
            
            x = self.w.head.weight @ self.layer_norm(x, self.w.ln_out)
            return x.float(), state

##########################################################################################################

def sample_logits(out, temperature=1.0, top_p=0.8):
    probs = F.softmax(out, dim=-1).numpy()
    sorted_probs = np.sort(probs)[::-1]
    cumulative_probs = np.cumsum(sorted_probs)
    cutoff = float(sorted_probs[np.argmax(cumulative_probs > top_p)])
    probs[probs < cutoff] = 0
    if temperature != 1.0:
        probs = probs.pow(1.0 / temperature)
    probs = probs / np.sum(probs)
    out = np.random.choice(a=len(probs), p=probs)
    return out

########################################################################################################

print(f'\nUsing CPU. Loading {args.MODEL_NAME} ...')
model = RWKV_RNN(args)

print(f'\nPreprocessing context (slow version. see v2/rwkv/model.py for fast version)')
init_state = None
for token in tokenizer.encode(context).ids:
    init_out, init_state = model.forward(token, init_state)

for TRIAL in range(NUM_TRIALS):
    print(f'\n\n--[ Trial {TRIAL} ]-----------------', context, end="")
    all_tokens = []
    out_last = 0
    out, state = init_out.clone(), init_state.clone()
    for i in range(LENGTH_PER_TRIAL):
        token = sample_logits(out, TEMPERATURE, TOP_P)
        all_tokens += [token]
        tmp = tokenizer.decode(all_tokens[out_last:])
        if '\ufffd' not in tmp: # only print when we have a valid utf-8 string
            print(tmp, end="", flush=True)
            out_last = i + 1
        out, state = model.forward(token, state)       
print('\n')
