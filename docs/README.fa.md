# Electronics AI — نسخهٔ ۰٫۷٫۰

[English](../README.md) · [فارسی](README.fa.md) · [简体中文](README.zh-CN.md) · [العربية](README.ar.md)

Electronics AI یک پلاگین محلی برای اتوماسیون طراحی PCB و شماتیک در Altium Designer است. پلاگین، اتصال `mcp-electronics-ai` و سه اسکیل راه‌اندازی، طراحی مدار و چیدمان PCB را با هم ارائه می‌دهد. مخزن اصلی [Electronics_AI](https://github.com/M-R-Abedini/Electronics_AI) است؛ نسخهٔ نصب‌شده و ZIP خروجی همین مخزن هستند.

**پیش‌نیاز:** ویندوز، Python 3.12 و نصب مجاز Altium Designer. آزمون‌های زندهٔ موجود روی Altium Designer 26 انجام شده‌اند؛ این به معنی تأیید نسخه‌های دیگر نیست.

## قابلیت‌ها

- خواندن و ویرایش شماتیک، PCB و کتابخانه‌های `.SchLib` و `.PcbLib`.
- برنامه‌ریزی چیدمان با عملکرد مدار، هندسهٔ واقعی پدها، قواعد مشروط هر بخش و مقایسه با چیدمان موجود. اعمال تغییرات از طرح ذخیره‌شده، با کنترل تازگی داده و بازخوانی نتیجه انجام می‌شود.
- تحلیل دوبعدی مس با `si_pi__`، طبقه‌بندی نت‌ها، پیشنهاد قواعد و نمایش محل تخلف؛ دادهٔ ناقص نتیجهٔ «قبول» نمی‌گیرد.
- جست‌وجو و دریافت کتابخانه‌های ابری با ثبت منبع و هش؛ صحت پکیج و نگاشت پین به پد همچنان باید بررسی شود.
- بکاپ فایل‌های ذخیره‌شده، صف مشترک فرمان‌های بومی و گزارش وضعیت هر عملیات.

این ابزار برای طراحی‌های مختلف است؛ قواعد تخصصی باید بر اساس عملکرد هر بخش و داده‌های همان پروژه انتخاب شوند. اعداد و چیدمان نمونه‌ها قاعدهٔ عمومی نیستند.

## نصب و راه‌اندازی

از محیط توسعهٔ آماده، بسته را بسازید و در Codex نصب کنید:

```powershell
.\.venv\Scripts\python.exe scripts/build_plugin.py --smoke --check-reproducible
codex plugin marketplace add .\dist\marketplace --json
$installedPlugin = codex plugin add mcp-electronics-ai@electronics-ai --json | ConvertFrom-Json
$pluginRoot = $installedPlugin.installedPath
py -3.12 "$pluginRoot\scripts\plugin_runtime.py" bootstrap --altium-exe "C:\Program Files\Altium\AD26\X2.EXE" --library-dir "D:\MyProject\Libraries"
py -3.12 "$pluginRoot\scripts\plugin_runtime.py" doctor --probe-backends
```

مسیر آلتیوم و کتابخانه را متناسب با سیستم خود تنظیم کنید. `bootstrap` محیط خصوصی مستقلی می‌سازد و بستهٔ EDA را به صورت wheel معمولی نصب می‌کند. تنظیمات خارج از کش پلاگین، در `~/.codex/plugins/data/mcp-electronics-ai` نگه‌داری می‌شوند. نام marketplace برابر `electronics-ai` است. پس از نصب یا ارتقا اتصال Codex را دوباره برقرار کنید.

فرمان‌های فعلی راه‌اندازی `bootstrap`، `doctor`، `configure-clients` و `serve` هستند. اجرای معمول MCP وابستگی نصب نمی‌کند. راهنمای کامل [بسته و تأیید نصب](PLUGIN_PACKAGE.md) و [نصب ویندوز و سایر کلاینت‌ها](INSTALLER.md) را ببینید.

## حدود اعتبارسنجی

موفقیت نصب یا دریافت فهرست ابزارها، اتصال بومی آلتیوم را ثابت نمی‌کند؛ آن اتصال باید جداگانه آزموده شود. چیدمان تأییدشده نیز به معنی روت کامل، DRC پاک یا آمادگی ساخت نیست. ویرایش دسته‌ای ممکن است پس از خطا نیمه‌کاره بماند؛ بکاپ فایل ذخیره‌شده جایگزین بازگردانی کامل بافر ذخیره‌نشده نیست. نتیجهٔ مبهم را پیش از تکرار بررسی کنید.

برای مسیرهای کاری، [مهارت چیدمان PCB](../skills/electronics-ai-pcb-layout/SKILL.md)، [تحلیل مس](COPPER_SI_PI.md) و [حفاظت از ویرایش‌ها](PRODUCT_SAFETY.md) را بخوانید. مجوزها در [اعلامیهٔ اجزای ثالث](../THIRD_PARTY_NOTICES.md) مشخص‌اند.
