# Free-Tier LLM Models & Limits

> Verified live on 2026-07-21 with my own keys. Limits researched from official docs + trackers — providers change these without notice, so treat as ballpark and check the official pages linked per section.

## Quick comparison

| Provider | Daily allowance | Rate limit | Best free model |
|---|---|---|---|
| Google Gemini | ~1,000–1,500 req/day (flash) | ~15 RPM | gemini-3.5-flash (1M ctx) |
| Groq | 1K–14.4K req/day (per model) | 30 RPM, 6K TPM | gpt-oss-120b |
| Cerebras | 1M tokens/day | 30 RPM, 60–100K TPM, 8K ctx cap | zai-glm-4.7 |
| OpenRouter | 50 req/day (1,000 after one-time $10 top-up) | 20 RPM | nemotron-3-ultra-550b:free |
| Cohere (trial) | 1,000 calls/MONTH total | 20 RPM chat | command-a-plus-05-2026 |
| Cloudflare | 10,000 neurons/day (~1,300 LLM responses) | shared neuron pool | glm-4.7-flash |
| Ollama cloud | GPU-time based, 5-hour sessions + weekly cap | concurrency-queued | kimi-k2.6 / glm-5.2 |

---

## Google Gemini (AI Studio key)

**Limits (free tier):**
- ~15 RPM, ~1,000–1,500 RPD per flash model (sources conflict; Google cut free quotas 50–80% in Dec 2025)
- ~250K TPM shared on 2.5-family models; RPD resets midnight Pacific
- Limits are per PROJECT, not per key — multiple keys don't add quota
- Free tier disappears if you enable billing on the project; unavailable in EU/UK/CH
- Live authoritative numbers: https://ai.google.dev/gemini-api/docs/rate-limits and https://ai.dev/rate-limit

**Free models (✅ = verified live by me):**
| Model | Ctx | Tools | Status |
|---|---|---|---|
| gemini-3.5-flash | 1M | yes | ✅ verified working |
| gemini-3-flash-preview | 1M | yes | ✅ verified working |
| gemini-3.1-flash-lite | 1M | yes | listed free on pricing page |
| gemini-2.5-flash / flash-lite | 1M | yes | likely free |
| gemini-2.0-flash / flash-lite | 1M | yes | likely free |
| gemma-4-31b-it / gemma-4-26b-a4b-it | 262K | yes | free |

**Paid-only (verified `limit: 0` on free tier):** gemini-3.1-pro-preview, gemini-3-pro-preview, gemini-2.5-pro. All Pro models removed from free tier April 2026.

---

## Groq

**Limits (free tier, org-level — multiple keys don't stack):**
- 30 RPM, ~6K TPM for most models (up to 30K TPM on some)
- Per-model RPD: llama-3.1-8b-instant 14,400/day (500K tokens/day); llama-3.3-70b 1,000/day; Whisper 2,000/day + 7,200 audio-sec/hour
- Cached tokens don't count toward limits
- Your account-specific numbers: https://console.groq.com/docs/rate-limits

**Free models (all on free tier):**
| Model | Ctx | Tools | Notes |
|---|---|---|---|
| gpt-oss-120b | 131K | yes | reasoning, very fast |
| llama-3.3-70b-versatile | 131K | yes | 1K req/day |
| qwen3.6-27b | 131K | yes | vision + reasoning |
| gpt-oss-20b | 131K | yes | reasoning |
| llama-3.1-8b-instant | 131K | yes | highest RPD (14.4K) |
| groq/compound, compound-mini | 131K | built-in web search | no custom tools |
| whisper-large-v3 / -turbo | — | — | speech-to-text |
| orpheus TTS, prompt-guard, safeguard-20b, allam-2-7b | — | — | specialist |

---

## Cerebras

**Limits (free tier):**
- 1M tokens/day, resets 00:00 UTC
- 30 RPM, 60–100K TPM
- Context capped at 8,192 tokens on free tier (big caveat for agent work)
- Official: https://inference-docs.cerebras.ai/support/rate-limits

**Free models (✅ all verified listed with my key):**
| Model | Tools | Notes |
|---|---|---|
| zai-glm-4.7 | yes | frontier-class, ultra-fast inference |
| gpt-oss-120b | yes | reasoning |
| gemma-4-31b | yes | mid-tier |

---

## OpenRouter (`:free` models)

**Limits:**
- 20 RPM on all :free models (never increases)
- 50 requests/day if never funded; 1,000/day permanently after a one-time $10 credit purchase
- Failed/429 requests still count against daily quota
- Negative balance blocks free models
- Official: https://openrouter.ai/docs/api_reference/limits

**Free models (13, ✅ verified list + live-tested nemotron-3-ultra):**
| Model | Ctx | Tools |
|---|---|---|
| nvidia/nemotron-3-ultra-550b-a55b:free ✅ | 1M | yes |
| nvidia/nemotron-3-super-120b-a12b:free | 1M | yes |
| nvidia/nemotron-3-nano-30b-a3b:free (+omni variant) | 256K | yes |
| nvidia/nemotron-nano-9b-v2:free, nano-12b-v2-vl:free (vision) | 128K | yes |
| google/gemma-4-31b-it:free, gemma-4-26b-a4b-it:free | 262K | yes |
| openai/gpt-oss-20b:free | 131K | yes |
| cohere/north-mini-code:free | 256K | yes |
| poolside/laguna-m.1:free, laguna-xs-2.1:free | 262K | yes |
| nvidia/nemotron-3.5-content-safety:free | 128K | no |

---

## Cohere (trial key)

**Limits (trial):**
- 1,000 API calls per CALENDAR MONTH across ALL endpoints combined — the tightest cap of any provider here
- Chat 20 RPM; Embed 5/min; Rerank 10/min
- Non-commercial use only
- Official: https://docs.cohere.com/docs/rate-limits

**Free (trial) models — all Cohere models accessible:**
| Model | Ctx | Tools | Notes |
|---|---|---|---|
| command-a-plus-05-2026 | 436K | yes | vision + reasoning, flagship |
| command-a-reasoning-08-2025 | 288K | yes | reasoning |
| command-a-03-2025 | 288K | yes | |
| command-r7b-12-2024, command-r/r-plus-08-2024 | 128K | yes | older |
| aya-expanse-32b, aya-vision-32b | 128K/16K | no | multilingual/vision |
| translate, transcribe, embed, rerank models | — | — | specialist |

---

## Cloudflare Workers AI (free plan)

**Limits:**
- 10,000 neurons/day shared across ALL models; resets 00:00 UTC; no overage on free plan
- ≈1,300 LLM responses/day on small models; big models burn neurons much faster
- Some models are paid-plan-gated entirely regardless of neurons
- Official: https://developers.cloudflare.com/workers-ai/platform/pricing/

**Verified with my key (account bdbba6e4...):**
| Model | Tools | Status |
|---|---|---|
| @cf/zai-org/glm-4.7-flash | yes | ✅ verified working on free plan (reasoning on) |
| @cf/zai-org/glm-5.2 | yes | ❌ requires Workers Paid plan ($1.40/$4.40 per M) — verified blocked |
| ~80 others (llama-3/4, mistral, qwen, gemma, gpt-oss, whisper, FLUX, embeddings) | varies | most small ones free |

Catalog endpoint: `GET /client/v4/accounts/{acct}/ai/models/search`

---

## Ollama Cloud (free tier)

**Limits:**
- No token/request numbers published — measured in GPU time
- Session limits reset every 5 hours + weekly limit per 7 days
- Models grouped level 1 (light, e.g. gpt-oss:20b) to level 4 (heavy, e.g. deepseek-v4-pro); stick to levels 1–2 to stretch quota
- Pro ($20/mo) = 50x free quota
- Official: https://ollama.com/pricing

**Free models (✅ all 18 verified listed with my key; OpenAI-compatible endpoint `https://ollama.com/v1`):**
| Model | Level (approx) | Notes |
|---|---|---|
| kimi-k2.6 / kimi-k2.7-code | heavy | frontier, code specialist |
| glm-5.1 / glm-5.2 | heavy | frontier (free HERE, paid on Cloudflare) |
| deepseek-v4-pro | 4 (extra-heavy) | frontier |
| deepseek-v4-flash | mid | fast |
| qwen3.5:397b, minimax-m3/m2.7/m2.5, mistral-large-3:675b | heavy | frontier/strong |
| nemotron-3-ultra / super / nano:30b | mixed | |
| gpt-oss:120b / gpt-oss:20b | mid / 1 | reasoning |
| gemma4:31b, kimi-k2.5 | mid | |

---

## Not usable

- **HuggingFace** — no key set
- **Google Pro models** — paid tier only

## Recommended free agentic stack (all verified live)

1. **gemini-3.5-flash** (Google) — 1M ctx workhorse
2. **zai-glm-4.7** (Cerebras) — fastest, but 8K ctx cap
3. **nemotron-3-ultra-550b:free** (OpenRouter) — biggest free frontier model, 1M ctx, 50 req/day
4. **gpt-oss-120b** (Groq) — fast reasoning + tools
5. **glm-5.2 / kimi-k2.7-code** (Ollama) — frontier quality, GPU-time budget
6. **glm-4.7-flash** (Cloudflare) — 10K neurons/day backup
7. **command-a-plus** (Cohere) — save the 1,000/month for special calls

## Sources

- [Gemini rate limits (official)](https://ai.google.dev/gemini-api/docs/rate-limits) · [Gemini free tier guide](https://www.aifreeapi.com/en/posts/gemini-api-free-tier-rate-limits) · [Gemini free tier 2026](https://pecollective.com/tools/gemini-free-tier-guide/)
- [Groq rate limits (official)](https://console.groq.com/docs/rate-limits) · [Groq free tier 2026](https://www.grizzlypeaksoftware.com/articles/p/groq-api-free-tier-limits-in-2026-what-you-actually-get-uwysd6mb) · [Groq free tier limits](https://tokenmix.ai/blog/groq-free-tier-limits-2026)
- [Cerebras rate limits (official)](https://inference-docs.cerebras.ai/support/rate-limits) · [Cerebras free tier](https://www.getaiperks.com/en/ai/cerebras-free-tier-guide)
- [OpenRouter limits (official)](https://openrouter.ai/docs/api_reference/limits) · [OpenRouter rate limits FAQ](https://openrouter.zendesk.com/hc/en-us/articles/39501163636379-OpenRouter-Rate-Limits-What-You-Need-to-Know)
- [Cohere rate limits (official)](https://docs.cohere.com/docs/rate-limits)
- [Cloudflare Workers AI pricing (official)](https://developers.cloudflare.com/workers-ai/platform/pricing/)
- [Ollama pricing (official)](https://ollama.com/pricing) · [Ollama Cloud free vs pro](https://dev.to/amareswer/ollama-cloud-free-vs-pro-usage-limits-pricing-what-you-actually-get-2026-3ieo)
