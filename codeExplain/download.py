import huggingface_hub
import os

# 设置 HF_ENDPOINT 环境变量
os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'

huggingface_hub.hf_hub_download(repo_id="BlinkDL/rwkv-4-pile-430m",filename='RWKV-4-Pile-430M-20220808-8066.pth', local_dir='./resource')
