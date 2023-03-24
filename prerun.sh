cp aliases.sh /root/.bash_aliases
cp .gitconfig /root/.gitconfig 
source aliases.sh

apt update
apt -y install tmux