# Embeddings

Status: first version (LOO-214), November 2026. Not released.

Zhunt serves embeddings from a model on this computer, so scripts that used Ollama's `nomic-embed-text` can point at Zhunt instead. Embeddings are **local only**: the text is sent to the local embedding server and never to a cloud provider.

## Set up

Run an embedding model (for example nomic-embed-text v1.5 GGUF) with an OpenAI-style server on this computer, then tell Zhunt where it is:

```
ZHUNT_LOCAL_EMBED_URL=http://127.0.0.1:8081/v1
```

The address must be on this computer (`127.0.0.1`, `::1` or `localhost`). Zhunt Plus starts embedding models with `--embeddings` automatically.

## Routes

| Route | Body | Answer | Key |
|---|---|---|---|
| `POST /api/embed` (Ollama) | `input`: a string or a list of strings | `{"model", "embeddings": [[...], ...]}`, unit-length vectors | none from this computer (same rule as the other Ollama routes) |
| `POST /api/embeddings` (Ollama, older) | `prompt`: a string | `{"embedding": [...]}`, **unscaled** vectors, as Ollama returns | none from this computer |
| `POST /v1/embeddings` (OpenAI) | `input`: a string or a list of strings | the server's OpenAI answer, unit-length vectors | master key |

If no embedding model is set up, or it is not answering, the answer is a 503 ending "nothing was sent to any cloud provider".

The `ollama` Python package works unchanged with `OLLAMA_HOST` set to Zhunt's address.

## Compatibility with vectors Ollama made

Checked 10 November 2026 against Ollama 0.23 with the same nomic-embed-text file:

- Same size (768) and the same scaling per route: unit length on `/api/embed`, unscaled (length ~23) on `/api/embeddings`.
- Text in lower case gives practically the same vectors (squared distance 0.001).
- **Text with capital letters does not.** nomic-embed-text uses an uncased BERT tokenizer, which lowercases text first. The llama.cpp server does this; Ollama does not, so for "Hello World" the two vectors have a cosine similarity of only 0.47. Zhunt's vectors follow the model's tokenizer. A store filled by Ollama from text with capitals should be re-embedded once when switching.
