# config for training GPT-2 (124M) down to very nice loss of ~2.85 on 1 node of 8X A100 40GB
# launch as the following (e.g. in a screen session) and wait ~5 days:
# $ torchrun --standalone --nproc_per_node=8 train.py config/train_gpt2.py

wandb_log = False
wandb_project = 'hande'
wandb_run_name = 'mini-gpt'

# these make the total batch size be ~0.5M
# 32 batch size * 5 gradaccum * 4 GPUs = 6400
batch_size = 32 #Number of slides to randomly choose
block_size = 128 #Number of patches from each slide to randomly choose
gradient_accumulation_steps = 5

#Number of disease types we're classifying
n_classes = 17

#Baby GPT
n_layer = 6
n_head = 8
n_embd = 512
dropout = 0.

learning_rate = 1e-4 # with baby networks can afford to go a bit higher
max_iters = 10000
lr_decay_iters = 10000 # make equal to max_iters usually
min_lr = 1e-6 # learning_rate / 10 usually
beta2 = 0.99

warmup_iters = 100 # not super necessary potentially

dataset = 'simclr-ciga512_10'

#Eval stuff
eval_interval = 100 # keep frequent because we'll overfit
eval_iters = 200
log_interval = 10 # don't print too too often

# weight decay
weight_decay = 1e-1