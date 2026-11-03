# گزارش P0 — Electronics AI 0.7.0

دامنهٔ این کار اتوماسیون عمومی طراحی PCB است. نام قطعات و بردهای مثال، الگوی ثابت چیدمان یا ثابت مهندسی عمومی ایجاد نمی‌کند. نسخهٔ ZIP قدیمی منبع تصمیم نیست؛ کد جاری مخزن، قرارداد ابزارها و شواهد اندازه‌گیری‌شده مبنا هستند.

## ۱. پایهٔ مخزن و یکپارچه‌سازی

پایهٔ main پیش از این تغییرات `3f2effa` و commit پایهٔ قابلیت placement برابر `ae9ddef` بود. مخزن shallow نیست. تغییرات موجود با حفظ index و working tree روی شاخهٔ کاری ادامه یافتند؛ پیش از تخت‌کردن فضای کاری، bundle تاریخچه و patchها تهیه شد. محل نهایی سورس `D:\project\Electronics_AI` است و هش ۸۱۸ فایل منتقل‌شده تأیید شد. [جزئیات پوشه‌ها](WORKSPACE_CONSOLIDATION.fa.md) موجود است.

نام محصول Electronics AI، مخزن `M-R-Abedini/Electronics_AI`، پلاگین/MCP برابر `mcp-electronics-ai` و marketplace برابر `electronics-ai` است. نسخهٔ انتشار این تغییرات `0.7.0` است. هدف حذف سازگاری‌های صرف با نسخه‌های قبلی و نصب یک مسیر اجرایی جدید و مستقل است. ادغام و push به main انجام شد و نصب جدید با آزمون واقعی تأیید شد؛ جزئیات و مرز اعتبار در بخش ۴ ثبت است.

## ۲. spike هندسه و پوشش داده

خواندن package geometry از برد صریح و از قبل بازشده انجام می‌شود. پدها با pose، اندازه، زاویه و لایهٔ واقعی خوانده می‌شوند؛ mirror سمت پایین و rotation در frame درست محاسبه می‌شوند. native فقط primitiveهای courtyard/body از لایه‌های صریح caller را برمی‌گرداند؛ mechanical layer خودکار courtyard فرض نمی‌شود. raw primitive یا bbox به courtyard معتبر ارتقا داده نمی‌شود. iteratorهای native در finally آزاد می‌شوند.

دو ایراد مانعِ تأیید کاذب با regression بسته شدند: coverage ناقص پد/outline/cutout hard unknown است؛ و پد آزاد در هندسهٔ ثابت برد باقی می‌ماند تا clearance و کانال بررسی شوند. مورد بازتولیدشدهٔ برخورد قطعه با پد آزاد در `(19,25) mm` رد می‌شود. اطلاعات ناقص نامرتبط stackup، در صورت کامل‌بودن صریح هندسهٔ لازم، placement را بی‌دلیل مسدود نمی‌کند.

## ۳. تغییرات Python و native

خطای compiler مربوط به `Stored` با نام `StoredManifest` اصلاح و lint آن افزوده شد. resolver برد بر مسیر صریح و هویت همان سند تکیه دارد. apply به طرح ذخیره‌شده، freshness هندسه/قواعد/قفل‌ها، checkpoint، شناسهٔ عملیات یکتا و readback وابسته است؛ optimizer هنگام apply دوباره اجرا نمی‌شود. نتیجهٔ نامعلوم یا partial خودکار تکرار نمی‌شود و native rollback تراکنشی ندارد.

سیاست جاری `placement-evidence-p0-v4` چهار Tier ثابت دارد: امکان‌پذیری سخت؛ فاصلهٔ Manhattan میان AABB پدها به‌اضافهٔ penalty صریح تغییر لایه و عدم پسرفت تک‌تک مسیرهای بحرانی/VCC/GND؛ HPWL وزندار سیگنال با exclusion صریح؛ و alignment با خانواده/grid/origin صریح. هیچ tolerance مثبت برای پنهان‌کردن پسرفت Tier1 وجود ندارد. crossing صرفاً diagnostic است و حداقل via یا routing کامل را اثبات نمی‌کند. تغییر صرفاً ظاهری baseline را جایگزین نمی‌کند.

snapshot دیسک buffer ذخیره‌نشده را اثبات نمی‌کند. `Document.Modified=False` به‌تنهایی گواه کامل clean بودن نیست. حالت صریح `disk_and_pose_evidence` فقط فایل ذخیره‌شده و poseهای اولیه را checkpoint می‌کند؛ autosave، side-flip native و بازیابی کامل buffer وعده داده نمی‌شوند. سقف فعلی batch native ۵۰۰ حرکت است.

## ۴. وضعیت آزمون‌ها و انتشار

| بررسی | شواهد و وضعیت |
|---|---|
| آزمون متمرکز protocol و هندسهٔ placement | ۱۰۴ موفق؛ شامل پد آزاد، coverage ناقص، تفاوت بریدگی مس/برد و مجوز overhang |
| benchmark سیاست v4 | ۱۵ حالت اصلی، ۵ خانواده، ۹ حالت تکمیلی، دو اجرای یکسان؛ [JSON](PCB_LAYOUT_BENCHMARK_RESULTS.json) |
| root کامل در CI | Windows: ۷۴۹ موفق؛ Ubuntu: ۷۲۷ موفق و ۲۲ skip پلتفرمی |
| eda-agent کامل در CI | Windows: ۴۷۱۹ موفق و ۱۴۲ skip؛ Ubuntu: ۴۷۰۶ موفق و ۱۵۵ skip محیط/فیکسچر |
| native build/lint | ۱۱ واحد، صفر خطا و هشدار؛ نسخهٔ `2026.11.03.electronics-ai-p0` |
| wheel مستقل نصب‌شده | ۱۴ عضو پروژهٔ native و هش ۲۱۶ فایل Python با سورس تطبیق دارد؛ نسخهٔ 0.7.0 و policy v4 |
| catalog نصب‌شده | ۵۱۹ قابلیت/۹ namespace + سه meta؛ schemaها و قرارداد ۳۴ ابزار کتابخانه بررسی شدند |
| بسته‌بندی | smoke و دو ساخت با ورودی یکسان موفق؛ SHA256 خروجی نهایی در `dist/electronics-ai-0.7.0/artifacts.json` ثبت می‌شود |
| main و GitHub | محصول در `b2c0e4a` و پل native در merge جداگانهٔ `31a4999` ادغام و push شدند؛ فقط main باقی است |
| نصب Codex | `mcp-electronics-ai@electronics-ai` نسخهٔ 0.7.0 فعال است؛ ثبت و کش قبلی حذف و دادهٔ قدیمی خارج از مسیر فعال آرشیو شد |
| پذیرش واقعی نسخهٔ نصب‌شده | PASS: پاسخ native، نسخه/هش پروژه، هندسهٔ پد، context تغییرناپذیر و رد هدف اشتباه؛ SHA256 فایل PCB تغییر نکرد |
| native placement apply | DEFERRED: پلن ذخیره‌شدهٔ واجد شواهد برای ویرایش نداریم؛ هیچ move/save یا آزمون replay و mutation روی برد اجرا نشد |

[CI کامل Windows و Ubuntu](https://github.com/M-R-Abedini/Electronics_AI/actions/runs/41650518818)
برای کد `10bf7aa` سبز است. tree ادغام `31a4999` دقیقاً با همان tree آزموده‌شده
برابر است. کامیت گزارش انتشار فقط شواهد و پاک‌کردن یک ignore منسوخ را اضافه می‌کند.
ساخت تکرارپذیر با ورودی یکسان در هر سیستم تأیید شد؛ یکسان‌بودن SHA بسته میان
Windows و Linux ادعا نمی‌شود. هیچ skip تازه‌ای برای عبور از شکست آزمون اضافه نشد.
فیکسچرها و وابستگی‌های غایب در شمارش skip قابل مشاهده‌اند.

گزارش خصوصی پذیرش نصب `analysis/final-installed-native-acceptance.json` و گزارش
مقایسهٔ wheel `analysis/final-isolated-wheel-audit.json` است. هندسه و مسیرهای خصوصی
برد در Git منتشر نشده‌اند. این آزمون‌های خواندنی، تأیید کیفیت placement یا routing
و DRC کامل نیستند. [روش benchmark](PCB_LAYOUT_BENCHMARKS.md) محدودیت‌ها را توضیح می‌دهد.

## ۵. blockerهای عمومی ورودی

input gate فقط با درخواست صریح caller، پروژه و PCB واقعی مشخص‌شده را بررسی می‌کند. نبود artifact یا ورودی ضروری، blocker قابل‌مشاهده ایجاد می‌کند؛ برد یا عدد ساختگی تولید نمی‌شود. درخواست isolation به دادهٔ همان طراحی از جمله ولتاژ، نوع عایق، transient، آزمون، creepage، clearance و استاندارد/ویرایش وابسته است. نبود داده safety با ثابت یک پروژهٔ دیگر پر نمی‌شود. این gate آمادگی ورودی را بررسی می‌کند و تأیید ایمنی محصول نیست.

## ۶. پذیرش ویندوز و مرز اعتبار

آزمون native باید با یک نمونهٔ آلتیوم، نسخهٔ script جاری، مسیر صریح و فایل آزمایشی یا کپی طراحی انجام شود. سلامت اتصال، target mismatch، snapshot حافظه، stale-plan refusal، checkpoint، عملیات تکراری و readback باید جداگانه ثبت شوند. هیچ برد تولیدی برای راحتی benchmark تغییر نمی‌کند.

در این اجرا کامپایل و پاسخ واقعی نسخهٔ جدید از داخل آلتیوم تأیید شد. روی یک
کپی PCB، هندسه و context از طریق MCP نصب‌شده خوانده شد و هدف بازنشده با
`ERR_TARGET_DOCUMENT_MISMATCH` رد شد. نبود courtyard واجد منبع و کانتور مکانیکی
پشتیبانی‌نشده به unknown باقی می‌مانند؛ برای گرفتن نتیجهٔ سبز داده ساخته نشد.

`fixture_U12` برابر `not_present_in_repo` است. آزمون native apply شامل stale plan،
checkpoint، حرکت قفل‌شده، رسید و replay زنده اجرا نشده و DEFERRED است.
[اسکریپت و چک‌لیست ویندوز](WINDOWS_PCB_ACCEPTANCE.md) آماده‌اند؛ اجرای پیش‌فرض
فقط خواندنی است و این مرز را صریح ثبت می‌کند. DRC کامل، routing تکمیل‌شده،
مقایسه با کارشناس، مدل سه‌بعدی و field simulation اجرا نشده‌اند.

کلاینت Codex برای بارگذاری نام MCP و اسکیل‌های جدید یک بار به Reconnect نیاز دارد.
محیط Python نصب‌شده مستقل است و به checkout توسعه یا کش قدیمی وابسته نیست.
