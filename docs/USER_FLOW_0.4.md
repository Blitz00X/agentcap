# AgentCap 0.4 — tek kullanıcı akışı sözleşmesi

**Tarih:** 13 Kasım 2026 · **Dal:** `quota-sources-0.4` · **Kapsam:** Claude Code
(Pro/Max) ve Codex CLI (ChatGPT planı) kota modu. Push/merge/tag/yayın yok.

Bu sözleşme, 2. karardaki ölçüm sözleşmesinin üstüne kurulur. Kullanıcının
gördüğü metin, iç durum, çıkış kodu ve sonraki adım burada tektir.

## 0. Dört koruma durumu

| Durum | Ne zaman | Kullanıcı metni | Koruma iddiası |
|---|---|---|---|
| `starting` | İlk okuma yok ve iş başlayalı ≤ 90 s | `waiting for the first reading` | Yok |
| `protected` | Taze okuma var; Pro | `protection active` | Evet, Pro durdurur |
| `protected` (Free) | Taze okuma var; Free | `watch only (Pro stops at the limit)` | Durdurma yok |
| `stale` | 90 s sonra hâlâ okuma yok, veya okuma `max_age`'den eski (Claude 60 s, Codex 600 s) | `data stale (protection not verified)` | Yok |
| `stopped` | Kota/döngü/hata/yanıt-zamanı durdurması | `stopped` | Durduruldu |

`stale` hiçbir yüzeyde "protection active" diye gösterilmez. Free hiçbir zaman
`protection active` demez.

## 1. Install

```bash
pipx install agentcap-cli
agentcap --version
```

```text
/plugin marketplace add Blitz00X/agentcap
/plugin install agentcap@agentcap
/agentcap:setup
```

Kurulum sonrası henüz kota koruması yoktur; `agentcap quota` salt okunur
çalışır. `/agentcap:unsetup` statusLine değişikliğini geri alır.

## 2. Start

```bash
agentcap run --quota 5% -- claude
agentcap run --quota 10% --window 5h -- claude
agentcap run --quota 5% -- codex
agentcap run --quota 10% --window 5h -- codex
```

```text
/agentcap:quota 5%
/agentcap:quota 2% 5h
```

İlk okumadan önce: `waiting for the first reading`.
Pro'da taze okuma: `protection active`.
Free'de taze okuma: `watch only (Pro stops at the limit)`.

Claude Code headless (`claude -p`) kota yüzdesi vermez; AgentCap bunu baştan
söyler ve yalnız döngü/hata kesicilerini çalıştırır:

```text
agentcap: WARNING: quota % is only supported in interactive Claude Code sessions.
Headless `claude -p` exposes no usage % to AgentCap, so this job will NOT be stopped
at the quota; the loop and error breakers are still active.
```

Kurulum/argüman hatası: çıkış 2. Okunamayan şema / guard hatası: çıkış 5.
Free'de `--quota` izler, durdurmaz; Pro'da durdurur.

## 3. Status

```bash
agentcap quota                  # canlı: codex app-server / claude -p /usage
agentcap quota --local          # yalnız yerel son okumalar
agentcap license status         # tier: free | pro
```

```text
/agentcap:status
```

Durum satırı dört durumdan birini taşır; ayrıca pencere, kullanım, yenilenme
zamanı ve `±1-3%` çözünürlük gösterilir. Yüzde hesap genelidir; paralel oturum
ve claude.ai/chatgpt.com kullanımı da sayılır. Eski okuma `STALE` işaretlenir;
yeniden okumak onu tazelemez.

## 4. Stop

| Yol | Çıkış | Kullanıcı metni / makbuz |
|---|---|---|
| Kota (Claude hook deny) | 3 | `stopped`; `the account's weekly quota rose X% while this job ran` |
| Kota (Codex SIGINT) | 3 | `stopped`; receipt `reason=quota_exceeded` |
| Döngü (aynı araç 4×) | 4 | `stopped`; `same tool call 4 times in a row (...)` |
| Hata (8 ardışık hata) | 4 | `stopped`; `8 failing tool calls in a row` |
| Codex yanıt zaman aşımı | 4 | `stopped`; `response_timeout` |
| Şema/kaynak kaybı (strict Codex) | 5 | `data stale (protection not verified)`; `share_pct=null` |
| Free gözlem | 0 | `watch only (Pro stops at the limit)`; `Pro's quota mode would have stopped this job at X%` |

Durdurma sonrası süreç ağacı temizlenir; terminal eski moduna döner. Makbuz
`0600`'dır ve yalnız sayı + araç adı taşır; istem/girdi yazılmaz.

## 5. Next step

**Claude Code (etkileşimli)**

Durdurma oturumu kapatmaz. Aynı oturumda:

```bash
agentcap approve          # +%50, en fazla 2 kez
```

```text
/agentcap:approve
```

`approve` kotayı %50 artırır (`5% -> 7.5% -> 11.25%`), döngü/hata kesicisini
temizler ve iş devam eder. Üçüncü onay reddedilir. SIGINT ile durmuş bir iş
`approve` ile diriltilemez; yeni iş gerekir. Ajan kendi kendine approve edemez;
komut ayrı terminalden veya kullanıcının slash komutundan gelmelidir.

**Codex CLI**

Durdurma SIGINT'tir; iş kapalıdır. `approve` "already stopped ... start it again
with a higher --quota" der. Yeni oturumda daha yüksek `--quota` ile başlatılır;
baseline yeni işte sıfırlanır, eski makbuz/ledger kalır, kota payı taşınmaz.

**Lisans geçişi**

İşin modu başlarken sabitlenir (`job.json` içindeki `enforce`). Free→Pro veya
Pro→Free geçişi **çalışan işi sessizce değiştirmez**; yeni tier bir sonraki işte
geçerlidir. `agentcap quota` ve `/agentcap:status` o anki tier'ı ayrıca gösterir.
Free işte `approve` reddedilir; Pro iş lisansını kaybederse o iş bitene kadar
durdurmaya devam eder, sonraki iş Free/watch-only olur. Bu, yarıda sessiz
koruma kaybını önler.

## 6. Yüzey eşlemesi

| Yüzey | Durum metni | Tier |
|---|---|---|
| `agentcap run` / runner özeti | `stopped`, `data stale...` | `enforce` satırı |
| `agentcap quota` | `STALE`, yaş, reset | — |
| Claude statusline (`agentcap_cc.py`) | dört durumdan biri | `Pro (enforcing)` / `Free (watch only)` |
| `/agentcap:status` | dört durum + tier | aynı |
| Makbuz | `state`, `state_text` | `enforce` |

## 7. Açık sınırlar

- Yüzde hesap geneli; tek yanıt bir adım gecikebilir; Codex/Claude tam sayı
  yüzde bildirir.
- `codex exec` ve Claude headless kota yüzdesi vermez.
- Codex rollout biçimi belgesizdir; şema değişirse fail-closed.
- Canlı Claude/Codex testleri hesap ve kota gerektirir; bu sözleşme yalnız
  davranışı tanımlar, canlı testi garanti etmez.
