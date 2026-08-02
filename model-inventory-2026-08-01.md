# Live Model Inventory — 2026-08-01

> Extracted live from all three endpoints (no auth needed for any of them):
> - `https://api.eaon.dev/v1/models` — callable eaon model ids (plain list)
> - `https://api.eaon.dev/v1/models/catalog` — eaon metadata (vendor/tier/tag)
> - `https://g4f.space/v1/models` — g4f proxy pool: (server, model) pairs **with request counts**
>
> Re-fetch: `curl -sS -H "User-Agent: study-bot/1.0" <url>` (blank UA gets Cloudflare-1010'd on eaon).

---

## 1. eaon — callable models (`/v1/models`, 64 ids + `auto`)

```
auto
gemini-3.1-flash-lite   gemma-4-31b-it         gemini-2.5-pro        deepseek-v3.2
deepseek-v4-pro         deepseek-v4-flash      glm-5.2               glm-5
grok-4.20-non-reasoning grok-4.20-reasoning    grok-4.3              grok-4.5
grok-code-fast-1        gpt-5                  gpt-5.3-codex         gpt-5.4
hermes-4-405b           hermes-4-70b           llama-3.3-70b-instruct minimax-m2.7
kimi-k2.6               nemotron-3-super       mimo-v2.5             mimo-v2.5-pro
deepseek-v3.1           gpt-5-codex            llama-4-scout         kimi-k2.7
gemini-3.1-pro-preview  gpt-image-2            qwen3-coder-480b-a35b-instruct
qwen3-235b-a22b-instruct agnes                gpt-5-nano            gemini-3
nova                    gpt-oss                qwen                  mistral
mistral-3.5             step-3.7               qwen-3.7              deepseek-v4
gemini-3.1-lite         gemini-3.5             nemotron              llama-3.1
minimax-m3              gemma-4                mercury               diffusion-gemma
kimi-k3                 gpt-5.5                sonnet-5              gpt-5.6-terra
gpt-5.6-sol             gpt-5.6-luna           opus-5                haiku-4.5
sonar                   gemini-3.1-pro         opc/nemotron-3-ultra-free
kai/nvidia/nemotron-3-ultra-550b-a55b:free
```

## 2. eaon — catalog (`/v1/models/catalog`, 65 entries)

Richer metadata per model: `id, apiModel, vendor, vendorName, name, tag, tagColor, description, tier`.
Tiers: **instant** (15) / **plus** (49) / **ultra** (1 — `gpt-5.5`).
Tags: flagship 17, fast 14, code 8, balanced 6, open 4, reasoning 4, standard 4, efficient 2, max/general/pro/image/preview/search 1 each.

**Mismatch between the two eaon endpoints:**
- In `/v1/models` but NOT catalog: `auto`, `deepseek-v4`, `gemini-3.1-lite`, `gemma-4`, `gpt-image-2`, `nemotron`
- In catalog but NOT callable in `/v1/models` (7 gated/extra entries): `llama-3.1-8b-hf`, `deepseek-r1-hf`, `qwen2.5-72b-hf`, `phi-3-mini-hf`, `k2.6-code-preview-free`, `coding-glm-5.2-free`, `gpt-image-2-free` (their `apiModel` fields carry the HF/upstream names)
- Two prefixed entries carry `accessTier: "plus"`: `opc/nemotron-3-ultra-free`, `kai/nvidia/nemotron-3-ultra-550b-a55b:free`
- Confirmed: eaon's health/tier info is **not reliable** for liveness (user-observed: models shown as working often dead). Only a live probe or request counts are trustworthy.

## 3. g4f — proxy pool (`/v1/models`, 156 pairs, 130 unique models)

Each entry: `{id: "<server>:<model>", owned_by: <upstream>, model, server, requests}`.
**`requests` = total requests routed through that (server, model) pair — the liveness/quality signal.**
Notable: **`eaon.dev` itself is an upstream in this pool** (g4f aggregates gateways), and `auto` = random public server.

Top 40 by total requests (across all servers of that model):

| model | total req | servers | best pairs (provider:req) |
|---|---|---|---|
| glm-5.2 | 1533 | 3 | ollama.pro:748, gen.pollinations.ai:740, eaon.dev:45 |
| llama-3.3-70b-versatile | 1504 | 1 | groq.com:1504 |
| minimax-m3 | 382 | 4 | ollama.com:312, ollama.pro:66, navy:2 |
| openai/gpt-oss-120b | 353 | 2 | groq.com:193, nvidia.com:160 |
| z-ai/glm-5.2 | 344 | 1 | nvidia.com:344 |
| gpt-4o-mini | 322 | 1 | api.airforce:322 |
| minimaxai/minimax-m3 | 283 | 1 | nvidia.com:283 |
| minimax-m2.7 | 278 | 1 | ollama.pro:278 |
| deepseek-ai/deepseek-v4-pro | 234 | 2 | nvidia.com:232, KTAI-junk:2 |
| gemma4:31b | 223 | 1 | ollama.com:223 |
| models/gemini-3-flash-preview | 217 | 1 | gemini-v1beta:217 |
| nemotron-3-super | 208 | 2 | ollama.com:205, ollama.pro:3 |
| deepseek-ai/deepseek-v4-flash | 208 | 2 | KTAI-junk:109, nvidia.com:99 |
| google/gemma-4-26b-a4b-it:free | 189 | 1 | openrouter.ai:189 |
| deepseek-v3.2 | 188 | 1 | crowllm.com:188 |
| nemotron-3-nano:30b | 187 | 2 | ollama.com:184, ollama.pro:3 |
| opus-4.7 | 178 | 1 | eaon.dev:178 |
| gemini-2.5-flash | 150 | 2 | Google Antigravity:140, gemini-v1beta:10 |
| zai-org/GLM-5.2 | 149 | 1 | community-day-2026:149 |
| gpt-oss:120b | 147 | 2 | ollama.pro:130, ollama.com:17 |
| deepseek-v4-pro | 141 | 2 | ollama.pro:139, navy:2 |
| kimi-k2.7-code | 116 | 2 | ollama.pro:114, navy:2 |
| nvidia/nemotron-3-nano-30b-a3b | 110 | 1 | nvidia.com:110 |
| gemini-3 | 103 | 1 | eaon.dev:103 |
| llama-3.1-8b-instant | 77 | 1 | groq.com:77 |
| deepseek-v3 | 76 | 1 | crowllm.com:76 |
| gpt-5.6-terra | 69 | 1 | eaon.dev:69 (composite-id pollution) |
| models/gemini-2.5-flash | 63 | 1 | gemini-v1beta:63 |
| nemotron-3-ultra | 61 | 1 | ollama.com:61 |
| meta/llama-3.1-70b-instruct | 60 | 1 | nvidia.com:60 |
| gpt-4o | 52 | 1 | api.airforce:52 |
| models/gemini-flash-lite-latest | 49 | 1 | gemini-v1beta:49 |
| gemini-3.1-pro-low | 46 | 1 | Google Antigravity:46 |
| models/gemini-3.1-flash-lite | 46 | 1 | gemini-v1beta:46 |
| gemini-3.1-flash-lite | 41 | 3 | gemini-v1beta:23, eaon.dev:17, crowllm.com:1 |
| gemini-3.6-flash | 39 | 1 | gemini-v1beta:39 |
| meta/llama-3.1-8b-instruct | 36 | 1 | nvidia.com:36 |
| moonshotai/Kimi-K2.7-Code | 32 | 1 | community-day-2026:32 |
| openrouter/free | 30 | 1 | openrouter.ai:30 |
| deepseek-v4-flash | 30 | 2 | eaon.dev:18, ollama.pro:12 |

(Full 156-row dump: re-fetch the endpoint; long tail is 1–30 requests and mostly noise.)

## 4. Data caveats (for the mapping step)

- **Polluted entries**: composite `srv_…:model` leaked into `model` fields, junk provider names ("KTAI - Free - Models (discord link)"), `unknown:` prefixes, `(+N)` suffixes, `.gguf` file names. Normalization needed before use.
- **Aliases galore**: `glm-5.2` also appears as `z-ai/glm-5.2`, `zai-org/GLM-5.2`; `models/gemini-*` (Google native prefix) vs bare ids; HF repo paths (`deepseek-ai/…`, `meta/…`) vs short names; `gemma4:31b` vs `gemma-4-31b-it`.
- Request counts are **cumulative pool totals**, not success rates — high traffic ⇒ working & used; low traffic ⇒ unused or flaky, can't distinguish "dead" from "unloved" without a live probe.

## 5. Overlap (normalized names) — 30 eaon models also visible in g4f pool

Already normalized for the mapping step. Highlights:

| eaon id | g4f pool total | best g4f pair |
|---|---|---|
| glm-5.2 | 2053 | ollama.pro:748 |
| minimax-m3 | 665 | ollama.com:312 |
| deepseek-v4-pro | 387 | nvidia.com:232 |
| deepseek-v4-flash | 241 | KTAI:109 |
| minimax-m2.7 | 278 | ollama.pro:278 |
| nemotron-3-super | 208 | ollama.com:205 |
| deepseek-v3.2 | 188 | crowllm.com:188 |
| gemini-3 | 103 | eaon.dev:103 |
| kimi-k2.7 | 25 | eaon.dev:25 |
| sonnet-5 | 26 | eaon.dev:26 |
| gpt-5.6-terra | 69 | eaon.dev:69 |

Strong g4f-only candidates (no eaon counterpart): `llama-3.3-70b-versatile` (1504),
`gpt-oss-120b` (517), `gpt-4o-mini` (322), `gemini-3-flash-preview` (223),
`gemini-2.5-flash` (213), `nemotron-3-nano-30b` (187), `opus-4.7` (178),
`kimi-k2.7-code` (148), `llama-3.1-8b-instant` (77).
