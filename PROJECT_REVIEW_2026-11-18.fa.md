# گزارش بررسی کامل پروژهٔ Electronics AI

- **تاریخ بررسی:** ۲۰۲۶-۱۱-۱۸
- **مخزن:** `M-R-Abedini/Electronics_AI` — شاخهٔ مبنا: `main` در commit `51e1df8`
- **روش بررسی:** خواندن کد و مستندات، بررسی همهٔ شاخه‌ها و تگ‌ها از طریق GitHub API (کلون محلی shallow بود)، بررسی تاریخچهٔ CI (۱۰۰ اجرای آخر)، اجرای واقعی هر دو مجموعه‌تست روی Linux/Python 3.11 با نصب وابستگی‌ها، و مقایسه با راه‌حل‌های موجود بازار.

> **نکتهٔ روش‌شناسی:** پروژه هدفش Windows + Python 3.12 + Altium Designer 26 است. بخشی از شکست‌های محلی (مشخصاً تست‌های `plugin_runtime` و نصاب) ناشی از گارد صریح «فقط Windows/3.12» در خود کد است و روی محیط هدف رخ نمی‌دهد؛ این موارد جدا علامت زده شده‌اند. اما شکست‌های CI روی خود GitHub (Windows و Ubuntu با Python 3.12) واقعی و مستقل از محیط من هستند.

---

## ۱) خلاصهٔ اجرایی

Electronics AI یک پلاگین دسکتاپ Codex / سرور MCP است که به یک مدل زبانی اجازه می‌دهد داخل Altium Designer زنده، شماتیک و PCB و فایل‌های کتابخانه (`.SchLib`/`.PcbLib`) را بخواند و ویرایش کند؛ روی آن لایه‌هایی از placement هندسی PCB، حسابرسی مس و SI/PI، زنجیرهٔ تأمین قطعات، مسیریابی (Freerouting) و تله‌متری اختیاری سوار شده و هستهٔ آن یک fork وندورشده از پروژهٔ متن‌باز `salitronic/eda-agent` است.

**حکم کلی:** از نظر جاه‌طلبی مهندسی، فرهنگ تست و مستندسازی شواهد، این پروژه بسیار بالاتر از میانگین پروژه‌های تک‌نفره است؛ اما در حال حاضر **در وضعیت «قرمز مزمن» قرار دارد**: CI از ۲۰۲۶-۱۱-۰۴ (نسخهٔ 0.8.0) تا امروز در **همهٔ اجراها شکست خورده**، چند رگرسیون واقعی در مسیر placement وجود دارد، دو وابستگی حیاتی (`shapely`/`scipy`) اصلاً در متادیتای بسته اعلام نشده‌اند، و پروژه با یک نفر توسعه‌دهنده، بدون محافظت شاخه و بدون Release منتشرشده برای نسخه‌های جدید، ریسک نگهداری بالایی دارد.

| بُعد | امتیاز (از ۵) | توضیح کوتاه |
|---|---|---|
| معماری و ایدهٔ محصول | ۴ | تفکیک gateway/engine/codec و شواهد‌محوری (evidence-bound) بالغ است |
| کیفیت و پوشش تست | ۳٫۵ | حجم تست عالی، ولی CI قرمز است و تست‌ها دیگر قابل اعتماد نیستند |
| سلامت CI/CD و انتشار | ۱٫۵ | CI بیش از دو هفته است کاملاً قرمز است؛ Release فقط تا v0.2.0 ساخته شده |
| نگهداشت‌پذیری کد | ۲٫۵ | فایل‌های غول‌پیکر (۱۲٫۹k خط)، fork وندور ۱۲۲k خطی، ۳۱۰۵ خطای ruff |
| امنیت | ۳٫۵ | الگوهای خوب زیاد است (allowlist، DPAPI، redaction)، ولی بدون CodeQL/Dependabot |
| مستندات | ۳ | حجم زیاد اما پراکنده، دوزبانهٔ ناهمگام و دارای drift نسخه |
| پایداری تیمی (Bus Factor) | ۱ | دقیقاً ۱ مشارکت‌کننده در کل تاریخچه |

---

## ۲) پروژه چیست؟ نقشهٔ معماری

```
MCP Client (Codex / Cursor / ...)
        │  stdio (JSON-RPC)
        ▼
suite_stdio.py ── gateway یکپارچه (۳ متا-ابزار در حالت minimal:
        │          suite_catalog / suite_invoke / suite_status)
        ├── eda__        → eda_stdio.py → bridge_coordination/engine_queue
        │                    → DelphiScript داخل Altium (فایل‌های .pas)
        ├── libraries__  → suite_libraries_native.py (کدک‌های OLE2 خالص پایتون)
        ├── cloud__      → suite_cloud_libraries.py (ChilaBoard / SnapMagic / SamacSys)
        ├── supply__     → suite_supply_chain.py (Nexar/DigiKey/Mouser/OEMsecrets)
        ├── route__      → suite_autoroute.py (Freerouting)
        ├── si_pi__      → suite_si_pi.py (حسابرسی مس و SI/PI)
        ├── layout__     → suite_layout.py → eda_agent.design (placement_evidence و …)
        └── telemetry__  → suite_telemetry.py (opt-in، پیش‌فرض خاموش)

eda-agent/ (fork از salitronic/eda-agent @ 1b60105، Apache-2.0)
  src/eda_agent/  bridge/ core/ design/ (placement و pipeline)
                  placer/ (موتور جایگذاری KiCad + Freerouting + Rust core)
                  tools/ (~۴۵۷ ابزار EDA)  web/ (داشبورد Flask)
  scripts/altium/*.pas  (۵۷k خط Pascal/DelphiScript)

crates/placer-core/  هستهٔ Rust (PyO3/maturin): هندسه، SA، grid routing — ۱٬۸۳۱ خط
skills/              سه اسکیل Codex (setup / circuit-design / pcb-layout)
installer/           نصاب Windows (Inno Setup)
```

**حجم کد (اندازه‌گیری‌شده):**

| بخش | حجم |
|---|---|
| Python کل مخزن (شامل تست) | ۲۲۳٬۸۴۹ خط در ۶۸۶ فایل |
| `eda-agent/src` | ۱۲۲٬۱۶۲ خط |
| تست‌های `eda-agent/tests` | ۷۴٬۰۸۰ خط |
| Pascal (DelphiScript) | ۵۷٬۵۲۷ خط |
| ریشه (`suite_*.py` و …) | ۱۱٬۵۸۹ خط |
| Rust (`placer-core`) | ۱٬۸۳۱ خط |
| Markdown | ۱۰۰ فایل / ~۱۳k خط |

---

## ۳) بررسی شاخه‌ها (درخواست صریح شما)

در گیت‌هاب ۵ شاخه وجود دارد. همه بررسی و با `main` مقایسه شدند (GitHub Compare API):

| شاخه | HEAD | نسبت به main | وضعیت واقعی |
|---|---|---|---|
| `main` | `51e1df8` | — | شاخهٔ اصلی، **بدون محافظت (protected: false)** |
| `feat/v2-f3` | `51e1df8` | ahead 0 / behind 0 | **دقیقاً برابر main** — شاخهٔ مرده، قابل حذف |
| `feat/v1.0-systematic-cluster-placement` | `23baa83` | ahead 0 / behind 64 | همان تگ `v1.0.0` — کامل merge شده |
| `feat/p2-copper-aware-cluster-and-si-audit` | `208908c` | ahead 0 / behind 66 | همان تگ `v0.9.0` — کامل merge شده |
| `feat/p1-live-apply-and-cluster-placer` | `b23227d` | ahead 0 / behind 67 | همان تگ `v0.8.1` — کامل merge شده |

نتایج جانبی:

- **۱۴ PR** در تاریخچه هست، هر ۱۴ تا merge شده‌اند؛ الان **۰ PR باز و ۰ Issue باز** وجود دارد.
- **۸ تگ** وجود دارد (`v0.2.0` تا `v2.0.0-alpha.2`) اما در بخش **GitHub Releases فقط یک Release (`v0.2.0`) منتشر شده**؛ یعنی کاربری که از صفحهٔ Releases دانلود می‌کند، هفت نسخه عقب است.
- کل تاریخچهٔ `main` فقط **۱۳۷ commit از یک نفر** (`M-R-Abedini`) است.
- نتیجه: هیچ کار گمشده یا merge‌نشده‌ای روی شاخه‌ها نیست (خبر خوب)، ولی شاخه‌های `feat/*` صرفاً کپی تگ‌ها هستند و فقط نویز ایجاد می‌کنند (خبر بد کوچک).

---

## ۴) نتایج اجرای واقعی تست‌ها (شواهد، نه حدس)

### ۴٫۱ تست‌های ریشه (`tests/`)

با نصب وابستگی‌ها (`pydantic`, `mcp`, `shapely`, `scipy`, …) روی Python 3.11:

- اجرای اول (بدون shapely/scipy): **832 پاس، 13 شکست، 22 skip**
- پس از نصب `shapely`/`scipy`: سه شکست محیطی رفع شد (از جمله `test_cold_gateway_imports_native_numeric_extension…` و تست بستهٔ پلاگین) و **۱۰ شکست باقی ماند** که در بخش ایرادها تک‌تک طبقه‌بندی شده‌اند.

### ۴٫۲ تست‌های `eda-agent/tests`

**4,796 پاس، 18 شکست، 170 skip — در ۶ دقیقه و ۴۷ ثانیه.**

### ۴٫۳ وضعیت CI در گیت‌هاب (مهم‌ترین شاهد)

- از ۱۰۰ اجرای آخر workflow «Suite tests»: **۶۴ شکست، ۳۶ موفقیت**.
- **آخرین اجرای موفق: ۲۰۲۶-۱۱-۰۴** (هم‌زمان با انتشار 0.8.0 روی `main` و شاخهٔ P1).
- از آن تاریخ تا امروز (از جمله آخرین commit روی `main`) **همهٔ اجراها قرمزند** و شکست در همان گام دوم رخ می‌دهد: `Offline stdio startup without an Altium installation`؛ در نتیجه بقیهٔ گام‌ها (از جمله کل تست‌ها) اصلاً اجرا نمی‌شوند و قرمزیِ فعلی، شکست‌های عمیق‌تر را هم پنهان کرده است.

---

## ۵) ایرادها

### 🔴 بحرانی (P0)

**۱ ـ CI بیش از دو هفته است کاملاً قرمز است و دیگر نقش دروازهٔ کیفیت را ندارد.**
ریشهٔ قابل اثبات: قرارداد شمارش ابزارها drift کرده است. `suite_si_pi.py` الان **۵ ابزار** برمی‌گرداند (`classify_and_partition`, `audit_copper`, `audit_copper_loops`, `synthesize_and_apply_rules`, و `focus_violation` که بعداً اضافه شده)، اما:
- `tests/smoke_suite_stdio.py:18` هنوز `COUNTS['si_pi'] = 4` را assert می‌کند ⇒ همان گام smoke در CI می‌شکند و کل pipeline می‌ایستد.
- `tests/test_si_pi_service.py:226` (`test_four_public_si_pi_contracts_validate`) هم روی `len(tools) == 4` می‌شکند.
یعنی یا ابزار پنجم بدون به‌روزرسانی قراردادها اضافه شده، یا تست‌ها عمداً به‌روز نشده‌اند؛ در هر دو حالت، از 0.9.0 به بعد هیچ گیت سبزی وجود نداشته تا رگرسیون‌های بعدی را بگیرد. این دقیقاً همان وضعیتی است که در آن باگ‌های بخش بعد بی‌صدا وارد `main` شده‌اند.

**۲ ـ وابستگی‌های `shapely` و `scipy` در هیچ متادیتای نصبی اعلام نشده‌اند.**
۱۶ ماژول در `eda_agent/placer/` (از جمله `eval/legality.py`, `eval/metrics.py`, `place/*`, `route/*`) در سطح ماژول `from shapely import …` و `from scipy … import …` دارند، اما:
- در `eda-agent/pyproject.toml` (بخش `dependencies`) نیستند،
- در `requirements.txt` / `requirements-runtime.txt` نیستند،
- در `constraints-windows-py312.txt` (فایل قفل نسخه‌ها) نیستند،
- فقط در `pixi.toml` (محیط توسعهٔ win-64) آمده‌اند.
پیامد عملی: هر نصب تمیز با pip (همان مسیری که README و CI استفاده می‌کنند) به محض import شدن `eda_agent.placer.mcp` با `ModuleNotFoundError: No module named 'shapely'` می‌میرد. این باگ را عملاً بازتولید کردم. اگر روی ماشین توسعهٔ شما کار می‌کند، فقط به این دلیل است که pixi آن‌ها را نصب کرده؛ کاربر نهایی و CI آن محیط را ندارند.

**۳ ـ سه رگرسیون واقعی و فعلی در مسیر placement (روی `main`):**

- **(الف) stackup ناقصِ نامرتبط، placement واجد شرایط را block می‌کند:**
  `test_unrelated_incomplete_stackup_does_not_block_qualified_placement_geometry` شکست می‌خورد؛ `feasible` برابر `False` با blockerهای `copper:coverage:0..4` و `copper:obstacles` برمی‌گردد، در حالی که نام و قرارداد تست (و منطق محصول) می‌گوید شواهد ناقصِ نامرتبط نباید جلوی هندسهٔ واجد شرایط را بگیرد. این یعنی در عمل، بردهایی که باید قابل جایگذاری باشند بی‌دلیل رد می‌شوند.
- **(ب) سقوط `KeyError: 'metrics'` در `placement_evidence.py:1056`:**
  در `compare_placement_candidates` روی `baseline["metrics"]` بدون هیچ guardای دسترسی زده می‌شود. تست بودجهٔ بچ native (`[500]` و `[501]` در `test_layout_review_regressions.py`) نشان می‌دهد وقتی evaluation بدون `metrics` برگردد (مسیر stub/خطا/ناقص)، کل `validate_plan` به جای برگرداندن gate تمیز، با exception سقوط می‌کند. در محصول واقعی این یعنی: به جای پیام «بودجهٔ انتقال native کافی نیست»، کاربر یک traceback می‌گیرد.
- **(ج) ترتیب gateها در handler پاسکال native درست نیست:**
  `test_native_gate_order_and_separate_immutable_operation_marker` روی نبودن `PCBM_EndModify` و `Finally PCBServer.PostProcess` در سورس handler پاسکال می‌شکند. یعنی عملیات جابه‌جایی native بدون براکت‌بندی کامل modify و بدون تضمین اجرای `PostProcess` در مسیر خطا انجام می‌شود؛ این دقیقاً با محدودیت اعلام‌شدهٔ خود README («undo grouping does not guarantee rollback») هم‌خوان است و ریسک خراب‌شدن بی‌صدای سند کاربر را بالا می‌برد.

**۴ ـ هستهٔ Rust (`placer_core`) در CI ساخته نمی‌شود و تست‌هایش به جای skip، fail می‌شوند.**
`crates/placer-core` نیازمند `maturin develop` است؛ در `.github/workflows/tests.yml` هیچ گام cargo/maturin وجود ندارد. نتیجه: `test_rust_cost_matches_python_reference` و ۹ تست placer دیگر (از جمله `test_solve_is_legal_better_and_deterministic`, تست‌های decap/rigid/copper-obstacle) با `ModuleNotFoundError: No module named 'placer_core'` می‌شکنند. پس یا باید CI هسته را بسازد، یا تست‌ها وقتی باینری نیست `skip` شوند؛ وضعیت فعلی یعنی معتبرترین بخش الگوریتمی پروژه (تطابق هزینهٔ Rust با مرجع Python) عملاً هیچ‌وقت در CI验证 نمی‌شود.

### 🟠 مهم (P1)

**۵ ـ drift نسخه و مستندات انتشار.**
`plugin.json` و `eda_agent/__init__.py` و `pyproject.toml` روی `1.0.0` هستند، اما `eda-agent/docs/RELEASE_VERIFICATION.md` هنوز نسخهٔ `0.8.0` را pin کرده؛ تست اختصاصی خودتان (`test_version_is_consistent`) دقیقاً برای همین می‌شکند و هشدار می‌دهد که کاربر یک نصب درست را «خراب» می‌خواند. در مخزنی که این‌همه روی شواهد و نسخه‌بندی دقیق حساس است، این drift نشانهٔ نبود «منبع واحد حقیقت» برای نسخه است.

**۶ ـ فایل‌های غول‌پیکر (God Files).**

| فایل | خط |
|---|---|
| `eda_agent/tools/easyeda.py` | ۱۲٬۸۹۳ |
| `eda_agent/tools/pcb.py` | ۵٬۷۳۵ |
| `eda_agent/tools/library.py` | ۴٬۶۲۳ |
| `eda_agent/design/pcb_placement.py` | ۳٬۰۴۱ |
| `eda_agent/tools/generic.py` | ۲٬۶۵۴ |
| `eda_agent/design/pipeline.py` | ۲٬۵۸۷ |
| `suite_libraries_native.py` (ریشه) | ۱٬۴۱۹ |
| `scripts/altium/PCB.pas` | ~۱۵k (۶۳۱KB) |

پیامد: مرور کد، تست واحدِ مجزا و merge بدون conflict عملاً سخت می‌شود و هر تغییر کوچک در `easyeda.py` ریسک رگرسیون در کل بک‌اند EasyEDA را دارد.

**۷ ـ fork وندورشدهٔ `eda-agent` در حال فاصله‌گرفتن از upstream است.**
طبق `UPSTREAM.json`، اسنپ‌شات از `salitronic/eda-agent` در commit `1b60105` گرفته شده؛ upstream اما زنده و فعال است (۳۷۱ ستاره، حدود ۶۰۰ ابزار، به‌روزرسانی مداوم [1](https://salitronic.com/eda-agent/) [2](https://skillsllm.com/skill/eda-agent)). شما ۱۲۲k خط از آن را داخل مخزن نگه می‌دارید و رویش تغییرات suite را سوار کرده‌اید (`MODIFICATIONS.md` با ۲۷۷ خط). هر ماه که می‌گذرد، گرفتن fixهای upstream (مخصوصاً fixهای کرش DelphiScript که خودتان در README «جاری و ادامه‌دار» توصیف کرده‌اید) گران‌تر می‌شود. الان هیچ مکانیزم دوره‌ای (حتی دستی و مستند) برای rebase روی upstream تعریف نشده است.

**۸ ـ نبود هرگونه اتوماسیون امنیتی و به‌روزرسانی وابستگی.**
در `.github/` فقط دو workflow هست (تست و نصاب دستی). نه Dependabot، نه CodeQL، نه اسکن secret. برای پروژه‌ای که: توکن‌های DigiKey/Nexar/Mouser را مدیریت می‌کند، از اینترنت فایل کتابخانه دانلود می‌کند، و روی ماشین کاربر کد اجرا می‌کند، این خلأ مهم است؛ مخصوصاً که `constraints-windows-py312.txt` نسخه‌ها را pin کرده و بدون Dependabot هیچ‌کس خبر نمی‌شود یک CVE در `cryptography` یا `pillow` آمده است.

**۹ ـ بدون محافظت شاخه، بدون Release، تک‌نفره.**
`main` محافظت ندارد (هر push مستقیم CI را قرمزتر می‌کند بدون اینکه کسی مجبور به سبزکردن شود)، از ۸ تگ فقط `v0.2.0` Release گیت‌هابی دارد، و کل پروژه یک مشارکت‌کننده دارد. ترکیب این سه یعنی: ریسک از دست رفتن دانش پروژه (bus factor = 1) و نبود هیچ نقطهٔ بازگشت رسمی برای کاربر.

**۱۰ ـ کیفیت ایستا (lint/type) عملاً رها شده است.**
هیچ فایل کانفیگ ruff در ریشه نیست و CI هیچ گام lintای ندارد (گام موسوم به «Lint and build…» فقط `build.py` را اجرا می‌کند). اجرای ruff پیش‌فرض: **۳٬۱۰۵ خطا** در کل مخزن (۲۱۵ تا فقط در فایل‌های ریشه). تست‌های بهداشت داخلی خود `eda-agent` هم قرمزند:
- `test_no_em_dashes`: ۱۹ مورد em/en-dash در `placer/intent/LABELS.md` و `place/mechanical.py`
- `test_unit_conversions_agree` و `test_units`: ثابت `MIL = 0.0254` خارج از `eda_agent.units` دوباره تعریف شده (`placer/baselines.py:26` و literal در `placer/io/altium.py:59` و `tools/pcb.py:1568`) — دقیقاً همان forki که ماژول `units` برای جلوگیری از واگرایی تبدیل واحد ساخته شده؛ در ابزار EDA، واگرایی mil/mm یعنی خطای فیزیکی روی برد.
- `test_docstring_references`: داک‌استرینگ `pcb_plan_placement` به ابزار ناموجود `design_intent` ارجاع می‌دهد و مدل زبانی را به بن‌بست می‌فرستد.
- `test_version_is_consistent` (بند ۵).

### 🟡 متوسط و کوچک (P2)

**۱۱ ـ تست‌های Windows-only بدون `skipif` روی Linux هم اجرا و fail می‌شوند.**
مثلاً `test_installer_contains_python_services_without_a_library_executable` روی Linux با `ValueError: Build on Windows x64 using Python 3.12` می‌شکند و تست‌های `plugin_runtime` هم روی هر Python غیر 3.12. روی CIِ Ubuntu (وقتی smoke درست شود) این‌ها قرمزی کاذب می‌سازند. الگوی درست (`skipif(sys.platform != 'win32')`) در همان فایل `test_plugin_runtime.py:260` استفاده شده ولی همه‌جا اعمال نشده است.

**۱۲ ـ سرعت بازخورد تست پایین است.**
مجموعهٔ `eda-agent` به‌تنهایی ۶:۴۷ دقیقه طول می‌کشد (تست ریشه ~۳۰ ثانیه است). با timeout چهل‌وپنج‌دقیقه‌ای CI مشکلی نیست، ولی برای توسعهٔ تک‌نفره یعنی چرخهٔ «تغییر → اطمینان» کند و وسوسهٔ ردکردن تست. هیچ تقسیم‌بندی fast/slow یا اجرای موازی (`pytest -n`) هم پیکربندی نشده است.

**۱۳ ـ وابستگی به مسیر و محیط توسعهٔ شخصی.**
`eda_agent/placer/corpus.py:23` مسیر پیش‌فرض `D:\project\example` را hardcode کرده (هرچند با env قابل تغییر است) و README مسیر متعارف سورس را `D:\project\Electronics_AI` اعلام می‌کند. روی ماشین هر کس دیگری، این‌ها یا بی‌اثرند یا گمراه‌کننده. همچنین `suite_cloud_libraries.py` و `suite_supply_chain.py` در بعضی مسیرها scheme `http` (بدون s) را هم می‌پذیرند؛ برای دانلود/ارسال دادهٔ کتابخانه و قطعات بهتر است فقط `https` مجاز باشد.

**۱۴ ـ مستندات زیاد ولی پراکنده و دوزبانهٔ ناهمگام.**
۱۰۰ فایل Markdown (~۱۳k خط) شامل گزارش‌های فارسی فازها (V2_ARCHITECTURE/PROGRESS/DECISION_LOG با صدها خط)، گزارش‌های review روزانه در `docs/review/` و README در ۴ زبان است. سند پیشرفت V2 هنوز F3 را «در حال اجرا» نشان می‌دهد در حالی که کد جلوتر رفته. این حجم مستندات بدون یک «صفحهٔ وضعیت واحد و به‌روز» عملاً خودش تبدیل به بدهی می‌شود: کاربر نمی‌داند کدام سند حرف آخر است.

**۱۵ ـ محدودیت‌های ذاتی محصول که باید شفاف‌تر مدیریت شوند (نقص نیست، ریسک محصول است):**
طبق README خودتان: فقط Altium Designer 26 تست زنده شده؛ انتقال Unicode کامل در JSON (مثل Ω یا متن چینی) reject می‌شود؛ ویرایش native ممکن است Altium را کرش کند و بعد از خطا تغییرات جزئی بدون rollback بماند؛ ECO شماتیک→PCB هنوز دیالوگ تعاملی می‌خواهد. این‌ها صادقانه مستند شده‌اند (نقطهٔ قوت)، ولی برای محصولی که قرار است به دست کاربر نهایی برسد، هرکدام باید در UI/پیام خطا هم همان‌قدر شفاف باشند، نه فقط در README.

---

## ۶) نقاط قوت (چیزهایی که نباید خراب شوند)

1. **فرهنگ شواهد و صداقت مهندسی کم‌نظیر است:** تقریباً هر قابلیت یک گزارش فارسی/انگلیسی با عدد بنچمارک، برد مرجع (StickHub و…)، شاهد اجرای زنده روی AD26 و فهرست صریح «چیزهایی که هنوز نمی‌دانیم» دارد (`docs/review/`، `REVIEW.md`، گزارش‌های P0/P1/V2). عبارت تکرارشوندهٔ «unknown evidence باقی می‌ماند» به جای ادعای دروغین، دقیقاً رفتار درست برای ابزار مهندسی است.
2. **حجم و کیفیت تست واقعاً بالاست:** ~۸۴۵ تست در ریشه و ~۴٬۹۸۰ تست در eda-agent (با skipهای مستند برای تست‌های نیازمند سخت‌افزار/نصب واقعی)، شامل تست‌های contract برای JSON Schema ابزارها، تست‌های property/round-trip برای کدک‌های SchLib/PcbLib و شبیه‌ساز Altium (`altium_simulator.py`).
3. **الگوهای امنیتی درست در جاهای حساس:** allowlist دامنه برای دانلود ابری (`api.github.com`/`raw.githubusercontent.com` و هاست‌های مشخص تأمین‌کنندگان)، ذخیرهٔ توکن با Windows DPAPI و عدم ذخیرهٔ plaintext (`suite_hub.py`)، redaction خودکار secretها در خروجی دیباگ (`suite_stdio.py`)، IPC در دایرکتوری per-user، request-ID همبسته برای نپذیرفتن پاسخ دیررس، و قفل FIFO برای کلاینت‌های هم‌زمان. این‌ها نشان می‌دهد تهدیدها واقعاً فکر شده‌اند.
4. **معماری gateway تمیز است:** حالت پیش‌فرض فقط ۳ متا-ابزار advertise می‌کند و بقیه از مسیر `suite_catalog` با اسکیما کشف می‌شوند؛ این هم context مدل را سبک نگه می‌دارد هم سطح حمله را کوچک می‌کند.
5. **مدل دادهٔ placement بالغ است:** intent صریح، پروفایل‌های composable، هندسهٔ پد transform‌شده، مقایسه با baseline و plan ذخیره‌شدهٔ اعتبارسنجی‌شده قبل از Apply، با checkpoint و readback؛ این از «مدل زبانی مستقیم روی برد دست می‌زند» چند پله جلوتر است.
6. **صفحه‌ها و گزارش‌های فارسی** (README فارسی، گزارش‌های فاز) برای مخاطب ایرانی پروژه یک مزیت واقعی دسترسی است، به شرط همگام‌سازی (بند ۱۴).

---

## ۷) راه‌حل‌های موجود در بازار و جایگاه شما

| راه‌حل | مدل | نسبت به Electronics AI |
|---|---|---|
| **salitronic/eda-agent** (upstream شما) | MCP متن‌باز برای Altium زنده (~۶۰۰ ابزار)، KiCad و EasyEDA هم دارد [1](https://salitronic.com/eda-agent/) | نزدیک‌ترین رقیب و هم‌زمان تأمین‌کنندهٔ کد شما؛ مزیت شما نسبت به آن: لایهٔ suite یکپارچه، کدک‌های native کتابخانه، placement شواهدمحور و SI/PI روی مس واقعی. ریسک: upstream سریع‌تر حرکت می‌کند و ممکن است همین قابلیت‌ها را بومی کند. |
| **altium-mcp (coffeenmusic)** و **altium-designer-mcp** | MCPهای سبک‌تر برای Altium (کوئری/کتابخانه، بعضی آفلاین) [3](https://jlcpcb.com/blog/altium-mcp-how-to-design-a-real-pcb-with-gpt-6-and-codex) | رقیب مستقیم ولی کم‌عمق‌تر؛ شما در audit و placement جلوترید، آن‌ها در سادگی نصب جلوترند. |
| **KiCad MCPها** (مثل blwfish/kicad-mcp با ۷۱ ابزار) | MCP برای KiCad رایگان، با Freerouting و DRC [1](https://github.com/blwfish/kicad-mcp) | بازار موازی ارزان/رایگان؛ اگر کاربر Altium نداشته باشد شما اصلاً گزینه نیستید. بک‌اند KiCad داخل eda-agent شما وجود دارد ولی در محصول suite برجسته نشده است. |
| **Flux** | ویرایشگر ابری PCB با ایجنت AI (از ایده تا Gerber) [2](https://buildpcb.tech/guides/best-ai-pcb-design-tools.html) | محصول ابری آماده برای غیرمتخصص؛ شما ابزار مهندس Altium‌کارید، نه جایگزین CAD. رقابت غیرمستقیم. |
| **Quilter / DeepPCB** | AI برای layout و routing بردِ شماتیک‌آماده (RL و شبیه‌سازی فیزیک) [2](https://buildpcb.tech/guides/best-ai-pcb-design-tools.html) [5](https://jlcpcb.com/blog/ai-tools-for-pcb-design) | رقیب مستقیم لایهٔ placement/routing شما؛ آن‌ها ابری و پولی‌اند، شما محلی و evidence-bound. مزیت شما: داخل Altium و بدون آپلود برد. ضعف شما: هنوز F3/F4 در V2 کامل نشده. |
| **JITX** | طراحی code-driven با قواعد مهندسی در کد [5](https://jlcpcb.com/blog/ai-tools-for-pcb-design) | فلسفهٔ متفاوت (تولید از spec)؛ برای تیم‌های نرم‌افزاری. شما روی برد موجود کار می‌کنید. |
| **Freerouting** (که استفاده می‌کنید) | autorouter متن‌باز DSN/SES | انتخاب درست و استاندارد؛ ولی طبق مستندات خودتان SES به‌تنهایی DRC-clean بودن در Altium را ثابت نمی‌کند، پس لایهٔ اعتبارسنجی خودتان مزیت است و باید نگهش دارید. |

**جمع‌بندی جایگاه:** در نیچ «ایجنت AI داخل Altium زنده، محلی، متن‌باز، با کنترل هندسی/شواهد» عملاً رقیب هم‌سطح ندارید؛ نزدیک‌ترین رقیب، upstream خودتان است. تهدید اصلی شما فنی نیست، **نگهداری** است: اگر CI قرمز بماند و fork از upstream عقب بیفتد، این مزیت ظرف چند ماه از دست می‌رود.

---

## ۸) پیشنهادها (نقشهٔ راه اولویت‌بندی‌شده)

### 🔴 همین هفته (P0) — برگرداندن اعتماد

1. **قرارداد شمارش SI/PI را یکدست کنید:** یا `focus_violation` را از `list_tools()` عمومی خارج و داخلی کنید، یا عدد را در هر سه جا (`suite_si_pi.py`، `smoke_suite_stdio.py:COUNTS`، `test_si_pi_service.py`) به ۵ برسانید و نام تست را اصلاح کنید. بعد یک تست contract اضافه کنید که شمارش smoke را از خود `list_tools()` مشتق کند تا دوباره drift نشود.
2. **`shapely` و `scipy` را به `eda-agent/pyproject.toml` و `constraints-windows-py312.txt` اضافه کنید** (با همان سیاست کف/سقف نسخهٔ بقیهٔ وابستگی‌ها) و یک تست «نصب تمیز در venv خالی + import همهٔ entrypointها» به CI اضافه کنید.
3. **CI را مرحله‌ای سبز کنید:** اول smoke، بعد تست ریشه، بعد تست eda-agent؛ تا سبز شدن کامل، گام‌های بعدی را `continue-on-error` نگذارید—برعکس، PR بدون CI سبز را merge نکنید. هدف: اولین CI سبز کامل تا پایان هفته.
4. **هستهٔ Rust را در CI بسازید** (`maturin develop` روی هر دو OS) یا تا آن روز تست‌های `placer_core` را با `pytest.importorskip("placer_core")` به skip تبدیل کنید تا قرمزی کاذب، قرمزی واقعی را پنهان نکند.
5. **سه رگرسیون placement را fix کنید:** (الف) blockerهای coverage نامرتبط را از gate سختیِ feasibility جدا کنید (unknown بماند ولی block نکند)؛ (ب) در `compare_placement_candidates` به جای `baseline["metrics"]` از `.get("metrics", {})` و مسیر خطای تمیز استفاده کنید و gate بودجهٔ بچ (۵۰۰) را **قبل** از مقایسهٔ هندسی اعمال کنید؛ (ج) در handler پاسکال، جابه‌جایی را داخل `PCBM_BeginModify/EndModify` با `try/finally PCBServer.PostProcess` براکت کنید.

### 🟠 دو تا چهار هفتهٔ آینده (P1) — کاهش بدهی ساختاری

6. **منبع واحد نسخه:** نسخه را فقط در یک فایل نگه دارید (مثلاً `pyproject.toml`) و `plugin.json`، `RELEASE_VERIFICATION.md` و `__init__.py` را در build از همان تولید/بررسی کنید؛ تست `test_version_is_consistent` باید در CI اجباری شود.
7. **برای هر تگ، GitHub Release بسازید** (خودکار در workflow، با checksum مثل نصاب) و شاخه‌های `feat/p1`، `feat/p2`، `feat/v1.0` و `feat/v2-f3` را حذف کنید (همه با تگ‌ها تکراری‌اند). روی `main` محافظت بگذارید: PR + CI سبز الزامی.
8. **Dependabot + CodeQL را فعال کنید** (هر دو برای مخزن عمومی رایگان‌اند) و pinهای `constraints-windows-py312.txt` را به چرخهٔ به‌روزرسانی ماهانه بسپارید.
9. **استراتژی upstream را مکتوب کنید:** یا هر ماه یک «هفتهٔ sync با eda-agent بالادست» با چک‌لیست در `MODIFICATIONS.md`، یا صادقانه اعلام کنید fork مستقل شده و دیگر sync نمی‌کنید. وضعیت فعلی (نه این، نه آن) بدترین گزینه است.
10. **فایل‌های غول‌پیکر را بر اساس مرز دامنه بشکنید:** اول `easyeda.py` (۱۲٫۹k) به زیرماژول‌های tools/emitter/audit و `pcb.py` به primitives/placement/audit؛ بدون تغییر رفتار، فقط با تست‌های فعلی به عنوان تور ایمنی (بعد از سبز شدن CI).
11. **ثابت تبدیل واحد را تک‌منبعی کنید:** `MIL` در `baselines.py` و literalها در `altium.py`/`pcb.py` را با `MM_PER_MIL` از `eda_agent.units` جایگزین کنید و تست‌های `test_units` را به گیت اجباری برگردانید. در ابزار EDA این «تمیزی کد» نیست، ایمنی فیزیکی است.
12. **تست‌های Windows-only را `skipif` کنید** و یک برچسب `slow` برای تست‌های سنگین eda-agent بگذارید تا CI پیش‌فرض زیر ~۳ دقیقه بماند و مجموعهٔ کامل شبانه اجرا شود.

### 🟡 فصل آینده (P2) — رشد محصول

13. **پوشش تست (coverage) را اندازه بگیرید** و فقط برای ماژول‌های بحرانی (کدک‌ها، `placement_evidence`، `bridge_coordination`) آستانه بگذارید؛ الان هیچ‌کس نمی‌داند کدام مسیرها اصلاً تست ندارند.
14. **یک صفحهٔ «وضعیت محصول» واحد و به‌روز** (مثلاً `STATUS.md`) داشته باشید که جای ده‌ها گزارش فاز را برای کاربر بگیرد: چه چیز کار می‌کند، چه چیز alpha است، آخرین نسخه و محدودیت‌ها. گزارش‌های تاریخی بمانند ولی به آن لینک شوند.
15. **بک‌اند KiCad را به مزیت تبدیل کنید:** چون KiCad رایگان است و بازار MCP آن فعال است، یک مسیر «بدون Altium» برای placement/score (که همین حالا در `placer/` دارید) می‌تواند ورودی کاربر و بنچمارک عمومی شما باشد، بدون اینکه تمرکز Altium از دست برود.
16. **برای سه محدودیت ذاتی محصول، پیام محصولی طراحی کنید:** کرش احتمالی DelphiScript (پیشنهاد خودکار checkpoint قبل از هر Apply)، نبود rollback تراکنشی (نمایش صریح diff قبل/بعد)، و reject شدن Unicode (پیام خطای فارسی/انگلیسی روشن به جای رد خام). این‌ها را در خود ابزار برگردانید، نه فقط در README.
17. **مشارکت‌کنندهٔ دوم پیدا کنید:** حداقل یک نفر برای review و یک نفر برای نگه‌داری Release؛ تا آن روز، یک `SECURITY.md` در ریشه و `CODE_OF_CONDUCT.md` اضافه کنید (الان فقط داخل `eda-agent` هست).

---

## ۹) جمع‌بندی نهایی

Electronics AI از نظر ایده، عمق مهندسی و فرهنگ «شاهد قبل از ادعا» یکی از جدی‌ترین پروژه‌های متن‌باز حوزهٔ AI+EDA است و در نیچ Altium عملاً بی‌رقیب است. اما امروز بزرگ‌ترین دشمن پروژه، بازار یا فناوری نیست؛ **قرمز بودن مزمن CI و drift بین کد، تست و مستندات** است که باعث شده سه رگرسیون واقعی بی‌صدا روی `main` بنشینند و هیچ‌کس متوجه نشود. خبر خوب این است که همهٔ ایرادهای بحرانی پیدا‌شده (قرارداد SI/PI، وابستگی‌های اعلام‌نشده، گیت‌های placement، بیلد Rust در CI) ظرف چند روز قابل رفع‌اند و به محض سبز شدن دوبارهٔ CI، بقیهٔ نقشهٔ راه (sync با upstream، شکستن God Fileها، Release منظم) شدنی می‌شود.

**سه کاری که اگر فقط همین‌ها انجام شود بیشترین اثر را دارد:**
۱) سبز کردن CI و قفل‌کردن merge روی آن — ۲) اعلام `shapely`/`scipy` و ساختن Rust در CI — ۳) ساختن Release برای نسخه‌های فعلی و حذف شاخه‌های تکراری.

---

*پیوست شواهد کلیدی: `tests/smoke_suite_stdio.py:18` در برابر `suite_si_pi.py:list_tools()` (۵ ابزار)؛ `eda-agent/pyproject.toml` (نبود shapely/scipy) در برابر ۱۶ import در `eda_agent/placer/`؛ `placement_evidence.py:1056`؛ اجراهای CI شمارهٔ `43771580851` (آخرین، قرمز) و آخرین موفقیت در ۲۰۲۶-۱۱-۰۴؛ مقایسهٔ شاخه‌ها با GitHub Compare API (همه ahead=0).*

---

## پیوست: اصلاحات اعمال‌شده در ۲۰۲۶-۱۱-۱۸ (شاخهٔ `arena/04dd0cd6-electronics-ai`)

همهٔ موارد 🔴 بخش ۸ (P0) به همراه چند مورد P1 کوچک که مستقیماً جلوی سبز شدن CI را می‌گرفتند، رفع و با اجرای واقعی تست تأیید شدند:

| # | اصلاح | فایل‌ها |
|---|---|---|
| ۱ | قرارداد شمارش ابزارها یکدست شد: SI/PI برابر ۵ و layout برابر ۹ در قرارداد smoke؛ namespace گمشدهٔ `placer` (۹ ابزار) به COUNTS اضافه شد، به enum پارامتر `backend` در `suite_catalog` اضافه شد (قبلاً در حالت minimal قابل کشف نبود) و در README مستند شد؛ یک تست نگهبان (`test_smoke_counts_match_si_pi_catalog`) اضافه شد تا drift بعدی در تست واحد گرفته شود، نه در CI | `tests/smoke_suite_stdio.py`، `tests/test_si_pi_service.py`، `suite_stdio.py`، `README.md` |
| ۲ | `shapely>=2,<3` و `scipy>=1.13,<2` به وابستگی‌های `eda-agent` اضافه شد؛ پین‌های Windows/Python 3.12 (`scipy==1.17.1`، `shapely==2.2.0` — وجود wheel `cp312-win_amd64` روی PyPI تأیید شد) به فایل قفل اضافه شد؛ probe بوت‌استرپ هم هر دو را بررسی می‌کند | `eda-agent/pyproject.toml`، `constraints-windows-py312.txt`، `scripts/plugin_runtime.py` |
| ۳/الف | رگرسیون stackup رفع شد: در `section_complete`، ناقص بودن aggregate دیگر به سکشن‌هایی که گزارش gap ندارند نسبت داده نمی‌شود؛ gap نام‌برده‌شده برای همان سکشن همچنان block می‌کند و نبود هرگونه شاهد همچنان unknown می‌ماند | `eda_agent/design/placement_copper.py` |
| ۳/ب | گیت بودجهٔ انتقال native (سقف ۵۰۰ حرکت) در `validate_plan` قبل از مقایسهٔ انتخاب اجرا می‌شود و برای بچ غیرقابل‌ارسال مقایسه اصلاً اجرا نمی‌شود؛ خطای قرارداد evaluator به جای traceback، blocker از نوع `current_tier_1_selection` می‌سازد (fail-closed)؛ `compare_placement_candidates` نبود `metrics` را با پیام روشن رد می‌کند؛ stub تست بودجه به قرارداد واقعی گزارش evaluator ارتقا یافت | `suite_layout.py`، `placement_evidence.py`، `tests/test_layout_review_regressions.py` |
| ۳/ج | handler پاسکال `PCB_ApplyPlacementPlan` حالا کل بچ را با یک جفت `PCBServer.PreProcess` و `Finally PCBServer.PostProcess` براکت می‌کند (هم‌الگو با بقیهٔ handlerهای بچ)؛ هر قطعه همچنان `PCBM_BeginModify/EndModify` خودش را دارد؛ به همین مناسبت `SCRIPT_VERSION` طبق قرارداد پروژه به `2026.11.18.electronics-ai-p4` bump و سند انتشار به‌روز شد | `PCB.pas`، `Main.pas`، `docs/RELEASE_VERIFICATION.md` |
| ۴ | CI هستهٔ Rust را قبل از تست‌ها می‌سازد (`pip install ./crates/placer-core` با toolchain پایدار)؛ بدون باینری، تست‌های placer به جای fail با skip شفاف رد می‌شوند (همچنین برای نبود toolchain پین‌شدهٔ KiCad/Freerouting)؛ پیام خطای `rust_problem` راهنمای بیلد دارد | `.github/workflows/tests.yml`، `tests/placer/conftest.py` (جدید)، `placer/place/problem.py` |
| ۵ | موارد hygiene مسدودکنندهٔ CI: ثابت `MM_PER_MIL` در هر سه سایت باقیمانده تک‌منبعی شد (`baselines.py`، `placer/io/altium.py`، `tools/pcb.py`)؛ ارجاع داک‌استرینگ به ابزار ناموجود `design_intent` اصلاح شد؛ em/en-dashهای `LABELS.md` و `mechanical.py` پاک شدند؛ تست‌های وابسته به Python 3.12 روی هاست‌های دیگر skip شرطی شدند | متفرقه |

**نتیجهٔ اجرای محلی پس از اصلاحات** (Linux، Python 3.11 — پروژه هدفش Windows/Python 3.12 است؛ تست‌های نسخه/پلتفرم‌محور به‌درستی skip شدند و روی CI اجرا می‌شوند):

- تست‌های ریشه: **841 پاس، 0 شکست، 27 skip**
- تست‌های eda-agent: **4,803 پاس، 0 شکست، 181 skip**
- گام smoke در CI (`tests/smoke_stdio.py` و `smoke_suite_stdio.py --toolset full`): **پاس** (همان گامی که از ۲۰۲۶-۱۱-۰۴ قرمز بود)
- بیلد bundle پاسکال (`build.py`، lint صفر خطا)، بیلد wheel و `--check-wheel`: **پاس**
- بیلد بستهٔ پلاگین با `--smoke --check-reproducible`: **پاس**

موارد باقی‌مانده از نقشهٔ راه (انجام‌نشده): محافظت شاخهٔ `main` و Release خودکار برای تگ‌ها، Dependabot/CodeQL، استراتژی sync با upstream، شکستن فایل‌های غول‌پیکر، و حذف شاخه‌های تکراری `feat/*` (نیازمند دسترسی admin گیت‌هاب).
