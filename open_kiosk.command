#!/usr/bin/env bash
# Double-click launcher (macOS/Linux): finds the Fire TV, connects and opens the kiosk URL.
cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
    python_cmd=python3
elif command -v python >/dev/null 2>&1; then
    python_cmd=python
else
    echo "Python não encontrado. Instale em https://www.python.org/downloads/"
    read -r -p "Pressione Enter para fechar..." _
    exit 1
fi

"$python_cmd" src/firetv_kiosk.py open
exit_code=$?
echo
if [ "$exit_code" -eq 0 ]; then
    echo "Tudo certo. Pode fechar esta janela."
else
    echo "Não foi possível abrir. Leia a mensagem acima."
fi
read -r -p "Pressione Enter para fechar..." _
exit "$exit_code"
