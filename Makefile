train:
		torchrun --standalone --nproc_per_node=4 train.py config/train_gpt2_wsi.py

search: 
		torchrun --standalone --nproc_per_node=4 train_optuna.py config/train_gpt2_wsi_optuna.py