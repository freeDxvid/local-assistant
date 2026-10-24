#!/bin/bash
# Doppelklick im Finder startet den Assistenten.
cd "$(dirname "$0")"
MODEL="${ASSISTENT_MODEL:-qwen2.5:3b}"

for app in "$HOME/Applications/Ollama.app" "/Applications/Ollama.app"; do
  [ -x "$app/Contents/Resources/ollama" ] && PATH="$app/Contents/Resources:$PATH"
done
if ! command -v ollama >/dev/null 2>&1; then
  echo "Ollama ist nicht installiert. Bitte zuerst von https://ollama.com/download installieren."
  read -r -p "Enter zum Beenden"; exit 1
fi

# Ollama-Dienst starten, falls er nicht schon läuft
if ! curl -s http://127.0.0.1:11434/api/tags >/dev/null; then
  (ollama serve >/dev/null 2>&1 &)
  for _ in $(seq 1 20); do curl -s http://127.0.0.1:11434/api/tags >/dev/null && break; sleep 0.5; done
fi

# Modell einmalig herunterladen (~2 GB)
ollama list | grep -q "^${MODEL}" || ollama pull "$MODEL"

(sleep 1; open "http://127.0.0.1:8765") &
ASSISTENT_MODEL="$MODEL" exec python3 assistent.py
