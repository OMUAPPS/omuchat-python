# export PATH="$PATH:$HOME/.rye/shims/"
directory="packages/"

for subdir in "$directory"/*; do
    if [ -d "$subdir" ]; then
        (cd "$subdir" && "$@")
    fi
done