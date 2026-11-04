# Electronics AI — الإصدار 1.0.0

[English](../README.md) · [فارسی](README.fa.md) · [简体中文](README.zh-CN.md) · [العربية](README.ar.md)

إضافة محلية لأتمتة تصميم المخططات ولوحات PCB في Altium Designer. تجمع اتصال
`mcp-electronics-ai` وثلاث مهارات للإعداد وتصميم الدوائر وتوضيع المكونات. المصدر
الأساسي هو مستودع [Electronics_AI](https://github.com/M-R-Abedini/Electronics_AI)؛
الحزمة المثبتة وملف ZIP ناتجان عن هذا المصدر.

**المتطلبات:** ويندوز، Python 3.12 ونسخة مرخصة من Altium Designer. الاختبارات
المباشرة المتاحة أجريت على Altium Designer 26؛ لا تثبت دعم الإصدارات الأخرى.

## الوظائف

- قراءة وتحرير المخططات وPCB ومكتبات `.SchLib` و`.PcbLib`.
- مقارنة خيارات التوضيع باستخدام وظيفة الدائرة وهندسة الوسادات الفعلية والقيود
  الخاصة بالمشروع. يتم تطبيق خطة محفوظة مع التحقق من حداثة البيانات وقراءة النتيجة.
- فحص هندسة النحاس ثنائية الأبعاد عبر `si_pi__`، وتصنيف الشبكات ومعاينة القواعد
  وتحديد موضع المخالفات. البيانات الناقصة لا تعني اجتياز الفحص.
- البحث في المكتبات السحابية مع تسجيل المصدر والبصمة؛ يلزم التحقق من الحزمة
  وترقيم الأرجل والوسادات بصورة مستقلة.
- نسخ احتياطية للملفات المحفوظة وطابور مشترك للأوامر الأصلية.

القواعد المتخصصة تُختار حسب وظيفة كل جزء وبيانات المشروع؛ تخطيط مثال سابق ليس
نموذجًا ثابتًا لكل اللوحات.

## التثبيت

من بيئة تطوير معدة، ابنِ الحزمة وثبت الإضافة:

```powershell
.\.venv\Scripts\python.exe scripts/build_plugin.py --smoke --check-reproducible
codex plugin marketplace add .\dist\marketplace --json
$installedPlugin = codex plugin add mcp-electronics-ai@electronics-ai --json | ConvertFrom-Json
$pluginRoot = $installedPlugin.installedPath
py -3.12 "$pluginRoot\scripts\plugin_runtime.py" bootstrap --altium-exe "C:\Program Files\Altium\AD26\X2.EXE" --library-dir "D:\MyProject\Libraries"
py -3.12 "$pluginRoot\scripts\plugin_runtime.py" doctor --probe-backends
```

استبدل مساري Altium والمكتبات بالمسارات المقصودة. ينشئ `bootstrap` بيئة Python
خاصة مستقلة ويثبت EDA كحزمة wheel عادية. الإعدادات خارج ذاكرة الإضافة المؤقتة،
في `~/.codex/plugins/data/mcp-electronics-ai`. اسم marketplace هو `electronics-ai`.
أعد توصيل Codex بعد التثبيت أو التحديث. التشغيل المعتاد لا يثبت التبعيات.

أوامر الإعداد الحالية هي `bootstrap` و`doctor` و`configure-clients` و`serve`.
راجع [دليل الحزمة والتحقق](PLUGIN_PACKAGE.md) و[مثبت ويندوز والعملاء](INSTALLER.md).

## حدود التحقق

نجاح التثبيت أو استعراض الأدوات لا يثبت اتصال Altium الأصلي. اجتياز التوضيع لا
يثبت اكتمال التوجيه أو سلامة DRC أو الجاهزية للتصنيع. قد يترك فشل التعديل الدفعي
تغييرات جزئية؛ نسخ الملفات المحفوظة لا تستعيد كل التعديلات غير المحفوظة. افحص
النتيجة الملتبسة قبل إعادة التنفيذ.

راجع [مهارة PCB](../skills/electronics-ai-pcb-layout/SKILL.md)،
[تحليل النحاس](COPPER_SI_PI.md)، [حماية التعديلات](PRODUCT_SAFETY.md)
و[التراخيص](../THIRD_PARTY_NOTICES.md).
