"""
This training script can be run both on a single gpu in debug mode,
and also in a larger training run with distributed data parallel (ddp).

To run on a single GPU, example:
$ python train.py --batch_size=32 --compile=False

To run with DDP on 4 gpus on 1 node, example:
$ torchrun --standalone --nproc_per_node=4 train.py

To run with DDP on 4 gpus across 2 nodes, example:
- Run on the first (master) node with example IP 123.456.123.456:
$ torchrun --nproc_per_node=8 --nnodes=2 --node_rank=0 --master_addr=123.456.123.456 --master_port=1234 train.py
- Run on the worker node:
$ torchrun --nproc_per_node=8 --nnodes=2 --node_rank=1 --master_addr=123.456.123.456 --master_port=1234 train.py
(If your cluster does not have Infiniband interconnect prepend NCCL_IB_DISABLE=1)
"""

import os
from glob import glob
import time
import math
import pickle
from contextlib import nullcontext

import optuna #For running param search
from optuna.trial import TrialState
import mlflow #For logging results
remote_server_uri = "http://dnb2.stjude.org:5678"

import numpy as np
import torch
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.distributed import init_process_group, destroy_process_group

from model import GPTConfig, GPTWSI

# -----------------------------------------------------------------------------
# default config values designed to train a gpt2 (124M) on OpenWebText
# I/O
out_dir = 'wsi_out_search'
eval_interval = 2000
log_interval = 1
eval_iters = 200
eval_only = False # if True, script exits right after the first eval
always_save_checkpoint = False # if True, always save a checkpoint after each eval
init_from = 'scratch' # 'scratch' or 'resume' or 'gpt2*'
# wandb logging
wandb_log = False # disabled by default
wandb_project = 'owt'
wandb_run_name = 'gpt2' # 'run' + str(time.time())
# data
dataset = 'openwebtext'
gradient_accumulation_steps = 5 # used to simulate larger batch sizes
batch_size = 12 # if gradient_accumulation_steps > 1, this is the micro-batch size
block_size = 1024
# model
n_layer = 12
n_head = 12
n_embd = 512
dropout = 0.0 # for pretraining 0 is good, for finetuning try 0.1+
bias = False # do we use bias inside LayerNorm and Linear layers?
# adamw optimizer
learning_rate = 6e-4 # max learning rate
max_iters = 600000 # total number of training iterations
weight_decay = 1e-1
beta1 = 0.9
beta2 = 0.95
grad_clip = 1.0 # clip gradients at this value, or disable if == 0.0
# learning rate decay settings
decay_lr = True # whether to decay the learning rate
warmup_iters = 2000 # how many steps to warm up for
lr_decay_iters = 600000 # should be ~= max_iters per Chinchilla
min_lr = 6e-5 # minimum learning rate, should be ~= learning_rate/10 per Chinchilla
# DDP settings
backend = 'nccl' # 'nccl', 'gloo', etc.
# system
device = 'cuda' # examples: 'cpu', 'cuda', 'cuda:0', 'cuda:1' etc., or try 'mps' on macbooks
#device = 'cpu' # examples: 'cpu', 'cuda', 'cuda:0', 'cuda:1' etc., or try 'mps' on macbooks
dtype = 'bfloat16' # 'float32', 'bfloat16', or 'float16', the latter will auto implement a GradScaler
compile = True # use PyTorch 2.0 to compile the model to be faster

#BL
n_classes = 17
search_id = 1
n_trials = 100

# -----------------------------------------------------------------------------
config_keys = [k for k,v in globals().items() if not k.startswith('_') and isinstance(v, (int, float, bool, str))]
exec(open('configurator.py').read()) # overrides from command line or config file
config = {k: globals()[k] for k in config_keys} # will be useful for logging
# -----------------------------------------------------------------------------

#Load all .pt files, and concat them into features dict and labels list
def load_wsi_features(data_dir):
    files = glob(os.path.join(data_dir, '*.pt'))
    slides_labels = []
    features = {}
    for file in files:
        data = torch.load(file)
        slide = os.path.basename(file).split('.')[0]
        label = np.unique(data['labels'])[0]
        slides_labels.append((slide, label))
        features[slide] = data['features']
    slides_labels = np.array(slides_labels)
    len_features = {k:len(features[k]) for k in features.keys()}
    return features, slides_labels, len(slides_labels), len_features
    
def suggest_params(trial):
    block_size = trial.suggest_categorical('block_size', [64, 128, 256, 512])
    n_layer = trial.suggest_int('n_layer', 4, 10)
    dropout = trial.suggest_float('dropout', 0.0, 0.2)
    weight_decay = trial.suggest_float('weight_decay', 0.0, 0.2) 
    learning_rate = trial.suggest_float('learning_rate', 1e-6, 1e-3, log = True)
    learning_rate_scale = trial.suggest_float('learning_rate_scale', 1, 100, log = True)
    min_lr = learning_rate / learning_rate_scale
    n_head = trial.suggest_categorical('n_head', [4, 8, 16])
    model__n_linear_layers = trial.suggest_int('model__n_linear_layers', 1, 2)
    model__mean_or_flatten = trial.suggest_categorical('model__mean_or_flatten', ['mean', 'flatten'])

    return {'block_size': block_size, 
            'n_layer': n_layer,
            'dropout': dropout,
            'weight_decay': weight_decay,
            'learning_rate': learning_rate,
            'learning_rate_scale': learning_rate_scale,
            'min_lr': min_lr,
            'n_head': n_head,
            'model__n_linear_layers': model__n_linear_layers,
            'model__mean_or_flatten': model__mean_or_flatten}

def objective(trial):

    trial = optuna.integration.TorchDistributedTrial(trial)

    train_data = load_wsi_features(data_dir)
    val_data = load_wsi_features(val_data_dir)

    if master_process:
        runs = [os.path.basename(x) for x in glob(os.path.join(out_dir, '*_ckpt.pt'))]
        if runs:
            max_run = max([int(x.split('_')[1]) for x in runs])
            run_no = max_run + 1
        else:
            run_no = 0
        out_file = os.path.join(out_dir, f'run_{run_no}_ckpt.pt')

    # if master_process:
    #     mlflow.pytorch.autolog(log_every_n_step = 150, log_models = False)
    params = suggest_params(trial)
    block_size = params['block_size']
    n_layer = params['n_layer']
    dropout = params['dropout']
    weight_decay = params['weight_decay']
    learning_rate = params['learning_rate']
    min_lr = params['min_lr']
    n_head = params['n_head']
    model__n_linear_layers = params['model__n_linear_layers']
    model__mean_or_flatten = params['model__mean_or_flatten']

    with mlflow.start_run() as run:

        # if master_process == 0:
        #     mlflow.log_param('patch_size', 512)
        #     mlflow.log_param('batch_size', batch_size)
        #     mlflow.log_param('n_classes', n_classes)
        #     mlflow.log_param('model_name', 'tranformer')
        #     mlflow.log_param('matmulprecision', dtype)
        #     mlflow.log_param('optimizer', 'adamw')

        start_time = time.time()

        # helps estimate an arbitrarily accurate loss over either split using many batches
        @torch.no_grad()
        def estimate_loss():
            out = {}
            model.eval()
            for split in ['train', 'val']:
                n_correct = 0
                losses = torch.zeros(eval_iters)
                for k in range(eval_iters):
                    X, Y = get_batch(split)
                    with ctx:
                        logits, loss = model(X, Y)
                    losses[k] = loss.item()
                    n_correct += torch.sum(torch.argmax(logits, dim = 1) == Y)
                out[split + '_acc'] = n_correct / (batch_size * eval_iters)
                out[split] = losses.mean()
            model.train()
            return out

        # learning rate decay scheduler (cosine with warmup)
        def get_lr(it):
            # 1) linear warmup for warmup_iters steps
            if it < warmup_iters:
                return learning_rate * it / warmup_iters
            # 2) if it > lr_decay_iters, return min learning rate
            if it > lr_decay_iters:
                return min_lr
            # 3) in between, use cosine decay down to min learning rate
            decay_ratio = (it - warmup_iters) / (lr_decay_iters - warmup_iters)
            assert 0 <= decay_ratio <= 1
            coeff = 0.5 * (1.0 + math.cos(math.pi * decay_ratio)) # coeff ranges 0..1
            return min_lr + coeff * (learning_rate - min_lr)

        #This will randomly choose a set of slides (batch_size)
        # and return a random selection of (block_size) patches for each element
        # x = (batch_size x block_size x feature_dim)
        # y = (batch_size x 1)
        def get_batch(split):
            data = train_data if split == 'train' else val_data
            features, slides_labels, n_slides, len_features = data
            
            assert n_slides > 0, f"No slides found in {split}, did the dataset load correctly?"

            #Randomly choose set of slides
            n_slides = len(slides_labels)
            batch_indices = np.random.choice(n_slides, size = batch_size, replace = True)
            slides = [s[0] for s in slides_labels[batch_indices]]
            labels = [s[1] for s in slides_labels[batch_indices]]
            sample_indices = [np.random.choice(len_features[s], size = block_size, replace = True) for s in slides]
            x = torch.stack([torch.from_numpy(features[s][sample_indices[idx],:]) for idx, s in enumerate(slides)])
            y = torch.from_numpy(np.array(labels, dtype = np.int64))

            if device_type == 'cuda':
                # pin arrays x,y, which allows us to move them to GPU asynchronously (non_blocking=True)
                x, y = x.pin_memory().to(device, non_blocking=True), y.pin_memory().to(device, non_blocking=True)
            else:
                x, y = x.to(device), y.to(device)
            return x, y
            
        # init these up here, can override if init_from='resume' (i.e. from a checkpoint)
        iter_num = 0
        best_val_acc = 0

        # attempt to derive vocab_size from the dataset
        meta_path = os.path.join(data_dir, 'meta.pkl')
        meta_vocab_size = None
        if os.path.exists(meta_path):
            with open(meta_path, 'rb') as f:
                meta = pickle.load(f)
            meta_vocab_size = meta['vocab_size']
            print(f"found vocab_size = {meta_vocab_size} (inside {meta_path})")

        # model init
        model_args = dict(n_layer=n_layer, n_head=n_head, n_embd=n_embd, block_size=block_size,
                        bias=bias, vocab_size=n_classes, dropout=dropout, n_linear_layers = model__n_linear_layers, mean_or_flatten = model__mean_or_flatten) # start with model_args from command line
        if init_from == 'scratch':
            # init a new model from scratch
            print("Initializing a new model from scratch")
            # determine the vocab size we'll use for from-scratch training
            if meta_vocab_size is None:
                print(f"defaulting to vocab_size of GPT-2 to {n_classes} ({n_classes} rounded up for efficiency)")
            model_args['vocab_size'] = meta_vocab_size if meta_vocab_size is not None else n_classes
            gptconf = GPTConfig(**model_args)
            model = GPTWSI(gptconf)
        elif init_from == 'resume':
            print(f"Resuming training from {out_dir}")
            # resume training from a checkpoint.
            ckpt_path = os.path.join(out_dir, 'ckpt.pt')
            checkpoint = torch.load(ckpt_path, map_location=device)
            checkpoint_model_args = checkpoint['model_args']
            # force these config attributes to be equal otherwise we can't even resume training
            # the rest of the attributes (e.g. dropout) can stay as desired from command line
            for k in ['n_layer', 'n_head', 'n_embd', 'block_size', 'bias', 'vocab_size']:
                model_args[k] = checkpoint_model_args[k]
            # create the model
            gptconf = GPTConfig(**model_args)
            model = GPTWSI(gptconf)
            state_dict = checkpoint['model']
            # fix the keys of the state dictionary :(
            # honestly no idea how checkpoints sometimes get this prefix, have to debug more
            unwanted_prefix = '_orig_mod.'
            for k,v in list(state_dict.items()):
                if k.startswith(unwanted_prefix):
                    state_dict[k[len(unwanted_prefix):]] = state_dict.pop(k)
            model.load_state_dict(state_dict)
            iter_num = checkpoint['iter_num']
            best_val_acc = checkpoint['best_val_acc']

        # crop down the model block size if desired, using model surgery
        if block_size < model.config.block_size:
            model.crop_block_size(block_size)
            model_args['block_size'] = block_size # so that the checkpoint will have the right value
        model.to(device)

        # initialize a GradScaler. If enabled=False scaler is a no-op
        scaler = torch.cuda.amp.GradScaler(enabled=(dtype == 'float16'))

        # optimizer
        optimizer = model.configure_optimizers(weight_decay, learning_rate, (beta1, beta2), device_type)
        if init_from == 'resume':
            optimizer.load_state_dict(checkpoint['optimizer'])

        # compile the model
        if compile:
            print("compiling the model... (takes a ~minute)")
            unoptimized_model = model
            model = torch.compile(model) # requires PyTorch 2.0

        # wrap model into DDP container
        if ddp:
            model = DDP(model, device_ids=[ddp_local_rank])

        # training loop
        X, Y = get_batch('train') # fetch the very first batch
        t0 = time.time()
        local_iter_num = 0 # number of iterations in the lifetime of this process
        raw_model = model.module if ddp else model # unwrap DDP container if needed
        running_mfu = -1.0
        while True:

            # determine and set the learning rate for this iteration
            lr = get_lr(iter_num) if decay_lr else learning_rate
            for param_group in optimizer.param_groups:
                param_group['lr'] = lr

            # evaluate the loss on train/val sets and write checkpoints
            if iter_num % eval_interval == 0 and master_process:
                losses = estimate_loss()
                print(f"step {iter_num}: train loss {losses['train']:.4f}, train acc {losses['train_acc']:.4f}, val loss {losses['val']:.4f}, val acc {losses['val_acc']:.4f}")
                if wandb_log:
                    wandb.log({
                        "iter": iter_num,
                        "train/loss": losses['train'],
                        "val/loss": losses['val'],
                        "lr": lr,
                        "mfu": running_mfu*100, # convert to percentage
                    })
                #For search, don't save checkpoint
                if losses['val_acc'] > best_val_acc or always_save_checkpoint:
                    best_val_acc = losses['val_acc']
                    if iter_num > 0:
                        checkpoint = {
                            'model': raw_model.state_dict(),
                            'optimizer': optimizer.state_dict(),
                            'model_args': model_args,
                            'iter_num': iter_num,
                            'best_val_acc': best_val_acc,
                            'config': config,
                        }
                        print(f"saving checkpoint to {out_dir}")
                        torch.save(checkpoint, out_file)
            if iter_num == 0 and eval_only:
                break

            # forward backward update, with optional gradient accumulation to simulate larger batch size
            # and using the GradScaler if data type is float16
            for micro_step in range(gradient_accumulation_steps):
                if ddp:
                    # in DDP training we only need to sync gradients at the last micro step.
                    # the official way to do this is with model.no_sync() context manager, but
                    # I really dislike that this bloats the code and forces us to repeat code
                    # looking at the source of that context manager, it just toggles this variable
                    model.require_backward_grad_sync = (micro_step == gradient_accumulation_steps - 1)
                with ctx:
                    logits, loss = model(X, Y)
                # immediately async prefetch next batch while model is doing the forward pass on the GPU
                X, Y = get_batch('train')
                # backward pass, with gradient scaling if training in fp16
                scaler.scale(loss).backward()
            # clip the gradient
            if grad_clip != 0.0:
                scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(model.parameters(), grad_clip)
            # step the optimizer and scaler if training in fp16
            scaler.step(optimizer)
            scaler.update()
            # flush the gradients as soon as we can, no need for this memory anymore
            optimizer.zero_grad(set_to_none=True)

            # timing and logging
            t1 = time.time()
            dt = t1 - t0
            t0 = t1
            if iter_num % log_interval == 0 and master_process:
                lossf = loss.item() # loss as float. note: this is a CPU-GPU sync point
                if local_iter_num >= 5: # let the training loop settle a bit
                    mfu = raw_model.estimate_mfu(batch_size * gradient_accumulation_steps, dt)
                    running_mfu = mfu if running_mfu == -1.0 else 0.9*running_mfu + 0.1*mfu
                print(f"iter {iter_num}: loss {lossf:.4f}, time {dt*1000:.2f}ms, mfu {running_mfu*100:.2f}%")
            iter_num += 1
            local_iter_num += 1

            # termination conditions
            if iter_num > max_iters:
                break

        run_name = run.info.run_name
        run_id = run.info.run_id
        artifact_path = run.info.artifact_uri
        duration = time.time() - start_time

        # if master_process:
        #     mlflow.log_metric('train_duration', duration)
        #     mlflow.log_param('artifact_path', artifact_path)
        #     mlflow_logger = MLFlowLogger(experiment_name=expt_name, tracking_uri=remote_server_uri)
        #     mlflow_logger._run_id = run_id    
        #     mlflow.log_artifacts('.', 'code')
        #     mlflow.pytorch.log_model(model, "model")
        #     model_path = os.path.join(artifact_path, 'model')

    return best_val_acc

if __name__ == "__main__":

    # WSI features
    #dataset = 'simclr-ciga512_10'
    data_dir = os.path.join('/data/comet-histology-ssl-features', dataset)
    val_data_dir = data_dir + '_val'

    mlflow.set_tracking_uri(remote_server_uri)
    expt_name = 'transformer_' + dataset
    mlflow.set_experiment(expt_name)

    # various inits, derived attributes, I/O setup
    ddp = int(os.environ.get('RANK', -1)) != -1 # is this a ddp run?
    if ddp:
        init_process_group(backend=backend)
        ddp_rank = int(os.environ['RANK'])
        ddp_local_rank = int(os.environ['LOCAL_RANK'])
        device = f'cuda:{ddp_local_rank}'
        torch.cuda.set_device(device)
        master_process = ddp_rank == 0 # this process will do logging, checkpointing etc.
        seed_offset = ddp_rank # each process gets a different seed
    else:
        # if not ddp, we are running on a single gpu, and one process
        master_process = True
        seed_offset = 0
        gradient_accumulation_steps *= 8 # simulate 8 gpus

    if master_process:
        os.makedirs(out_dir, exist_ok=True)

    torch.manual_seed(1337 + seed_offset)
    torch.backends.cuda.matmul.allow_tf32 = True # allow tf32 on matmul
    torch.backends.cudnn.allow_tf32 = True # allow tf32 on cudnn
    device_type = 'cuda' if 'cuda' in device else 'cpu' # for later use in torch.autocast
    # note: float16 data type will automatically use a GradScaler
    ptdtype = {'float32': torch.float32, 'bfloat16': torch.bfloat16, 'float16': torch.float16}[dtype]
    ctx = nullcontext() if device_type == 'cpu' else torch.amp.autocast(device_type=device_type, dtype=ptdtype)

    study_name = f'{dataset}_{search_id}'
    storage_name = "sqlite:///{}.db".format(study_name)

    study = None
    if master_process:
        study = optuna.create_study(direction="maximize", 
                                    study_name = study_name, 
                                    storage = storage_name,
                                    load_if_exists = True)
        study.optimize(objective, n_trials=n_trials)
    else:
        for _ in range(n_trials):
            try:
                objective(None)
            except optuna.TrialPruned:
                pass

    if master_process:
        assert study is not None
        pruned_trials = study.get_trials(deepcopy=False, states=[TrialState.PRUNED])
        complete_trials = study.get_trials(deepcopy=False, states=[TrialState.COMPLETE])

        print("Study statistics: ")
        print("  Number of finished trials: ", len(study.trials))
        print("  Number of pruned trials: ", len(pruned_trials))
        print("  Number of complete trials: ", len(complete_trials))

        print("Best trial:")
        trial = study.best_trial

        print("  Value: ", trial.value)

        print("  Params: ")
        for key, value in trial.params.items():
            print("    {}: {}".format(key, value))

        cmd = f"git add {study_name}.db; git commit -m 'Update repo'; git push origin"
        print("Sync db with git repo:")
        print(cmd)
        os.system(cmd)

    if ddp:
        destroy_process_group()
