# فهرست محل‌های نسخهٔ سیاست در مبنای P1

مبنای ممیزی: `cda116fe4bfddc0379b3ea4926cba9c1896812b3`.
این فهرست تمام فایل‌های tracked در HEAD مبنا را برای POLICY_VERSION، policy_version و placement-evidence-p0/p1 جست‌وجو می‌کند؛ فایل‌های باینری و analysis خصوصی منبع سیاست محسوب نشده‌اند. گزارش‌های جدید این ممیزی، مصرف‌کنندهٔ جدید سیاست ایجاد نمی‌کنند.

۲۹ رخداد در 10 فایل:

| فایل | خطوط در HEAD مبنا |
| --- | --- |
| `PCB_PLACEMENT_REPORT.fa.md` | 34 |
| `docs/P0_FOUNDATION_REPORT.fa.md` | 21 |
| `docs/PCB_LAYOUT_BENCHMARKS.md` | 62 |
| `docs/PCB_LAYOUT_BENCHMARK_RESULTS.json` | 5398 |
| `eda-agent/src/eda_agent/design/placement_evidence.py` | 20, 804, 1018, 1211, 1230 |
| `eda-agent/src/eda_agent/design/session.py` | 184, 191, 194, 240 |
| `scripts/benchmark_pcb_layout.py` | 26, 272 |
| `suite_layout.py` | 20, 262, 345, 394, 421, 448, 457, 475, 478, 488, 562 |
| `tests/test_layout_protocol.py` | 216 |
| `tests/test_layout_review_regressions.py` | 11, 119 |

دو ثابت اجرایی در suite_layout.py:20 و eda-agent/src/eda_agent/design/placement_evidence.py:20 هستند. session.py نسخه را در journal ذخیره/بازپخش می‌کند. benchmark از ثابت core وارد می‌کند و JSON بنچمارک نسخهٔ ثبت‌شده را دارد. test_layout_protocol.py:216 عمداً نسخهٔ قدیمی نامعتبر می‌سازد تا رد سیاست کهنه را بررسی کند؛ این نسخهٔ جاری نیست.

در مبنا selector ورودی policy_version وجود ندارد؛ get_contracts آن را در خروجی اعلام می‌کند. این محدودیت قبل از گام ۲ گزارش شد و سیاست/قرارداد در این مرحله تغییر نکرد.
