# پیشرفت Electronics AI 2.0

> تنها سندی که در طول اجرا به‌روز می‌شود. مرجع: [معماری](V2_ARCHITECTURE.fa.md) · [پلن اجرایی](V2_EXECUTION_PLAN.fa.md) · [دفتر تصمیم‌ها](V2_DECISION_LOG.fa.md).

| نسخه | فاز | وضعیت | تاریخ پایان | گیت‌ها |
|---|---|---|---|---|
| alpha.1 | F0a | تمام شد (commit `ba3cdb3`) | 2026-11-07 | ۷ از ۷ روی StickHub |
| alpha.1 | F0b | تمام شد؛ در انتظار بازبینی تو و تأیید commit | 2026-11-07 | ۵ از ۵ |
| alpha.1 | F0c | — | — | — |
| alpha.2 | F1 | — | — | — |
| alpha.3 | F2 | — | — | — |
| alpha.3 | F3 | — | — | — |
| alpha.4 | F4 | — | — | — |
| alpha.4 | F5 | — | — | — |
| beta.1 | F6 | — | — | — |
| rc.1 | F7 | — | — | — |
| rc.1 | F8 | — | — | — |
| 2.0.0 | F9 | — | — | — |

## گزارش فازها

### F0a · گیت سنجش روی StickHub (2026-11-07، شاخهٔ `feat/v2-f0a`)

اجرا: `pixi run route-check <board.kicad_pcb>` ⇒ `run_report.json` نسخهٔ ۰ در `.runtime/route_check/...`.

| گیت (پلن F0a) | مقدار StickHub | آستانه | نتیجه |
|---|---|---|---|
| نقض DRC از سهم router | ۰ | ۰ | ✅ |
| نقض تازهٔ غیرروتی | ۰ | ۰ | ✅ |
| حسابرسی: روت‌شده ۲۲۰ + zone ۰ + پشتیبانی‌نشده ۰ + روت‌نشده ۶ | ۲۲۶ | ۲۲۶ | ✅ |
| هر روت‌نشده با علت | ۶ از ۶ | همه | ✅ |
| مخرج پایدار (پیش و پس از import) | ۲۲۶ | ۲۲۶ | ✅ |
| دو اجرای پیاپی | SES یکسان `b078884b…` و علت‌های یکسان | یکسان | ✅ |
| زمان کل مسیر | ۲۱۷ s (۴ اجرای موازی) | ≤ ۳۰۰ s | ✅ |

پوشش (گیت این فاز نیست): **۹۷٫۴٪**، ۳۶ via، ۷۲۲ mm، همهٔ ترک‌ها 0.15 mm. روت‌نشده‌ها: ۴ پایهٔ USB کنار U1 (`via_blocked`)، `+3.3V` و `VBUS_SENSE` (`path_blocked`).

شاهد اضافه (همان پیکربندی، همهٔ گیت‌ها پاس):

| برد | پوشش | via | نقض router | زمان | نکته |
|---|---|---|---|---|---|
| pic_programmer (`neck` = 0.25) | ۱۰۰٪ | ۰ | ۰ | ۸۴ s | ۱۹ keepout متن مسی؛ قطعی در دو اجرا |
| pic_programmer (`neck` = 0) | ۹۵٫۲٪ | ۰ | ۰ | ۱۱۱ s | آزمون A/B |
| ecc83 | ۱۰۰٪ | ۰ | ۰ | ۸۳ s | — |

کد: `placer/toolchain.py`، `placer/report.py`، `placer/route/{dsn,freerouting,drc,check,__main__}.py`، helper بازنویسی‌شده؛ ۷ آزمون واحد + ۱ آزمون کند (`ELECTRONICS_AI_SLOW=1`)؛ ruff تمیز. تغییرات سازوکار: دفتر تصمیم‌ها بخش ۶ (۸ ردیف F0a).

### F0b · زیرساخت IR، سنجش و اسکلت MCP (2026-11-07، شاخهٔ `feat/v2-f0b`)

| گیت (پلن F0b) | نتیجه | آستانه | |
|---|---|---|---|
| رفت‌وبرگشت معنایی روی ۸ برد | ۸ از ۸ بدون هیچ فرق، هم «همان چیدمان» و هم «جابه‌جایی + چرخش ۹۰° + flip همه و برگرداندن» | بدون تغییر | ✅ |
| benchmark کامل dev | ۲۱٫۷ دقیقه (۵ برد × ۳ baseline، با روت، ۴ موازی) | ≤ ۳۰ دقیقه | ✅ |
| legality چیدمان متخصص | همهٔ یافته‌ها داوری شد (جدول پایین)؛ هیچ باگ IR باقی نماند | استثنای متخصص یا باگ رفع‌شده | ✅ |
| crate Rust | `placer-core` با MSVC ساخته و `placer_core.hpwl` از Python فراخوانده شد | ساخته و فراخوانده | ✅ |
| `placer__score` روی StickHub | از راه پروتکل MCP (stdio): گزارش + گالری، ۲۴۷ s با روت | گزارش و گالری | ✅ |

**داوری یافته‌های legality روی چیدمان متخصص (dev):**

| برد | یافته | داوری |
|---|---|---|
| stickhub | ۵۰ همپوشانی courtyard | استثنای متخصص: پروژه `courtyards_overlap = ignore` دارد (JSTها و خازن‌ها) |
| stickhub | J2، J5، J6، JP1 کمی بیرون از outline (≤ ۵٪) | استثنای متخصص: کانکتور و جامپر لبهٔ برد |
| pic_programmer | ۸ کانکتور بیرون از outline (J1 ۴۴٪) | استثنای متخصص: کانکتورهای لبه |
| royalblue | ۱۱ همپوشانی؛ H1 تا H4 و J1 تا J5 کمی بیرون | استثنای متخصص؛ همپوشانی‌ها **دقیقاً** همان ۱۱ جفت DRC کیکد است |
| video | L6 با L3 و J4 | تقریب IR: L6 در فایل courtyard ندارد و از پدها ساخته شده (برچسب «derived»)؛ کانکتورهای بیرون‌زده قفل‌اند |
| ecc83 | P4 ۱۱٪ بیرون | استثنای متخصص: کانکتور لبه |

**benchmark dev** (بودجهٔ Freerouting ۳۰۰ s برای همه؛ پوشش):

| برد | متخصص | random_legal | v1_constructor |
|---|---|---|---|
| stickhub | ۹۷٫۴٪ | ۶۲٫۶٪* | ۶۲٫۸٪* |
| pic_programmer | ۱۰۰٪ | ۱۰۰٪ | ۷۲٫۸٪* |
| royalblue | ۷۱٫۰٪* | ۴۷٫۳٪* | ۴۸٫۷٪* |
| video | ۲۴٫۳٪* | ۲۳٫۹٪* | timeout چیدمان (> ۶۰۰ s) |
| ecc83 | ۱۰۰٪ | ۱۰۰٪ | ۱۰۰٪ |

\* Freerouting به بودجهٔ ۳۰۰ s رسید. روی video و royalblue حتی چیدمان متخصص همگرا نشد، پس عدد پوشش این دو برد را بودجهٔ router تعیین می‌کند (دفتر تصمیم‌ها، ردیف ریسک F0b؛ تصمیم در DG-F1).

ساخته‌شده: IR نسخهٔ ۱ (`ir.py`) و adapter KiCad از راه pcbnew، استنتاج نقش لایه و صدور plane در DSN، `eval/` (legality، HPWL، تقاطع ratsnest، RUDY، حلقه‌های بحرانی v0، نظم، Pro Score v0 کالیبره‌نشده)، `render.py` (گالری)، `bench.py` و `corpus.py` و `baselines.py`، `mcp.py` (`placer__inspect`، `placer__score`؛ ثبت در `suite_stdio.py` و `.mcp.json` برای Claude Code)، crate `crates/placer-core`. آزمون‌ها: ۱۵ واحد + ۹ کند (`ELECTRONICS_AI_SLOW=1`)، همه پاس؛ ruff تمیز. تغییرات سازوکار: دفتر تصمیم‌ها بخش ۶ (ردیف‌های F0b).

گالری‌ها: `.runtime/f0b/bench_dev/<board>/gallery.html` و `.runtime/placer_runs/StickHub-*/gallery.html`.
