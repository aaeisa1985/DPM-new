# مواصفات منصة DPM الخفيفة

**SPEC v1.1 | مشروع مستقل لمدير محافظ واحد | أكتوبر 2026**

هذا المستند مواصفة تنفيذية كاملة ومكتفية ذاتيا، محدثة بعد مراجعة خارجية أغلقت فجوات النسخة الأولى. أي قرار معماري لازم للمطور موجود في هذه الصفحات أو في قسم القرارات المحسومة.

---

## 0. القرارات المحسومة

هذه القرارات ملزمة في v1. أي تغيير فيها قرار مالك المشروع وحده.

| # | القرار | القيمة |
|---|---|---|
| D1 | كيفية التنفيذ | لا تنفيذ آلي ولا تسجيل آلي من الخطة. الخطة تولد أوامر، والمدير يدخل التنفيذ الفعلي لكل أمر: كمية و سعر و رسوم، مع حالات filled / partial / cancelled. لا يدخل سجل الصفقات أي رقم تقديري أبدا |
| D2 | تفويض وحساب | علاقة 1 إلى 1 في v1: كل حساب يتبع تفويضا واحدا، والتفويض له حساب واحد. القيود في القاعدة تفرض ذلك |
| D3 | التسوية | SELL لا يفرج سيولته فورا. لكل ورقة مالية settlement_days: افتراضي 1 للأسواق الأمريكية و 2 للإماراتية، قابل للتعديل لكل ورقة. محرك إعادة التوازن يعد النقد المتاح فقط: نقد حر + متحصلات بيع مستقرة بتاريخها |
| D4 | أيام الإجازة والأسواق المغلقة | التقييم بأحدث إغلاق سابق (carry-forward) وموسوم في التقرير بأنه منقول، وليس تقديرا ولا فشلا. الفشل الوحيد: لا يوجد أي إغلاق سابق للورقة بتاتا |
| D5 | مصدر أسعار الإمارات والسندات والصكوك | استيراد CSV يدوي هو المصدر من الدرجة الأولى. Yahoo أفضل جهد للأسهم والصناديق الأمريكية فقط. الورقة تحدد مصدرها في جدولها |
| D6 | لغة الواجهة والرسائل | إنجليزي LTR في v1 (مصطلحات مالية قياسية). العربية مؤجلة كسيناريو مستقبلي |
| D7 | التكرار في الصفقات | fail-closed: مفتاح idempotency يشمل fx_rate و external_ref، والصفقة المطابقة تماما لنفس اليوم ترفض بخطأ صريح يعرض رقم الصفقة الأصلية. لا دمج صامت أبدا |
| D8 | النقد في الأوزان | weights_json يشمل مفتاح CASH صريحا، ومجموع كل الأوزان بما فيها النقد = 1.0 بالضبط. الانحراف و إعادة التوازن يقيسان على نفس الإجمالي، فلا تناقض بين الهدف والقياس |
| D9 | تخزين الأرقام | كل الأعمدة النقدية والكميات TEXT عبر TypeDecorator بـ 8 منازل عشرية، والواجهة البرمجية ترجع المبالغ نصوصا في JSON. لا عمود REAL لأي مبلغ مالي |
| D10 | حماية سجل التدقيق | triggers في SQLite ترفض UPDATE و DELETE على audit_log، مع سلسلة hash: كل سطر يحمل hash السطر السابق |

---

## 1. نظرة عامة

### 1.1 الهدف

أداة مكتب محلية خفيفة لمدير محافظ واحد، تدير محافظ عملاء متعددين تحت تفويضات استثمارية: إدخال الصفقات و التدفقات النقدية، محاسبة الحصص بدقة، قياس الانحراف، خطط إعادة توازن تحترم السيولة و التسوية، وأداء TWR.

### 1.2 المبادئ الحاكمة

1. عملية واحدة تعمل بأمر واحد: `uvicorn backend.app.main:app`
2. لا سيرفرات مساندة إلزامية
3. fail-closed: الفشل يعلن نفسه، ولا رقم مزيّف ولا دمج صامت
4. كل شيء يعمل دون إنترنت باستثناء الأسعار الحية الأمريكية
5. التراخيص: Apache / BSD / MIT فقط، فحص مباشر ومتعدٍ في CI
6. الحصص ليست جدول حالة: تعاد من الصفقات في كل تقييم، والسجل المصدر الوحيد للحقيقة

### 1.3 خارج النطاق (لا يبنى هنا أبدا)

بوابة عملاء، أدوار و موافقات، CRM، أوامر آلية على الوسيط، تقارير بعلامة بيضاء. هذه ملك منصة ويلزجيت.

### 1.4 معايير النجاح القابلة للقياس

| المعيار | القيمة |
|---|---|
| دقة TWR مقابل سيناريو مرجعي محسوب يدويا في fixtures | تفاوت أقل من 1e-8 |
| خطط إعادة توازن تكسر حد النقد المتاح | صفر، والاستحالة تعلن بخطأ InfeasiblePlanError |
| أسعار مزيّفة أو دمج صفقات صامت | صفر |
| إقلاع الخدمة لجاهزة الاستخدام على جهاز عادي | أقل من 3 ثوان باستيراد كسول للمكتبات الثقيلة |
| تغطية اختبار المحركات الستة | 90% فأعلى |

---

## 2. المعمارية والتراخيص

### 2.1 الرصة التقنية

| المكون | الاختيار | ترخيصه |
|---|---|---|
| اللغة | Python 3.12 | PSF |
| خدمة الويب | FastAPI + uvicorn | MIT |
| ORM و الترحيل | SQLAlchemy 2 + Alembic | MIT |
| القاعدة | SQLite، WAL مفعل، foreign_keys مفروضة | Public Domain |
| التحقق | pydantic v2 + pydantic-settings | MIT |
| الجداول | pandas، استيراد كسول داخل دوال الاستيراد فقط | BSD |
| التحسين | Riskfolio-Lib، استيراد كسول | BSD-3 |
| بيانات السوق | yfinance (أفضل جهد) + استيراد CSV أساسي + OpenBB V5 اختياري | Apache-2.0 |
| القوالب | Jinja2 | BSD |
| PDF لاحقا | WeasyPrint | BSD |
| الفحص | pytest, ruff, mypy, pip-audit, pip-licenses, pytest-cov | متساهلة |
| رفع الملفات | python-multipart | Apache-2.0 |
| HTTP client | httpx | BSD |

الاعتماد ملف lock بتثبيت hash كامل (uv lock أو pip-tools). كل مكتبة ثقيلة (pandas و Riskfolio و cvxpy) تستورد كسولا داخل الدوال التي تستخدمها فقط، وليس على مستوى الوحدات عند الإقلاع.

### 2.2 هيكل المجلدات

```
dpm/
  backend/
    app/
      main.py
      core/
        config.py
        db.py
        money.py          # Decimal، ROUND_HALF_UP، منازل لكل عملة
        security.py        # فحص Host و Origin و token محلي
      models/              # جدول لكل ملف
      engines/
        accounting.py
        pricing.py
        performance.py
        drift.py
        rebalance.py
        optimizer.py        # استيراد كسول ل Riskfolio
      api/
        routes_bootstrap.py # العملاء و التفويضات و الأوراق و الحسابات و التدفقات و fx و الأسعار
        routes_book.py      # الصفقات و الأحداث و الإلغاء
        routes_perf.py      # الأداء و الانحراف
        routes_plans.py     # إعادة التوازن و التنفيذ
        routes_io.py        # الاستيراد و التصدير و النسخ
        routes_audit.py
      ui/
        templates/
      services/
        csv_io.py
        backup.py           # VACUUM INTO و فحص checksum
    migrations/              # Alembic
  tests/
    test_accounting.py
    test_pricing.py
    test_performance.py
    test_drift.py
    test_rebalance.py
    test_optimizer.py
    test_csv_io.py
    test_backup.py
    test_api.py
    test_security.py
    test_golden.py
  data/
    dpm.db                  # git-ignored
  .env.example
  requirements.txt
  requirements.lock
```

---

## 3. الإعدادات

| المتغير | الافتراضي | الوصف |
|---|---|---|
| DPM_DB_PATH | ./data/dpm.db | مسار القاعدة |
| DPM_BIND | 127.0.0.1 | عنوان الربط، لا يغير إلا بعلم |
| DPM_LOCAL_TOKEN | فارغ | لو معبأ، كل طلب كتابة يلزمه هيدر X-DPM-Token بنفس القيمة، يطبع في الكونسول عند الإقلاع |
| DPM_PRICE_TTL | 300 | ثوان صلاحية السعر الحي في الكاش |
| DPM_PLAN_TTL | 900 | صلاحية خطة إعادة التوازن |
| DPM_SLIPPAGE_CAP | 0.02 | سقف انزلاق التنفيذ، علامة لا منع |
| DPM_DUST_VALUE | 100 | أقل قيمة أمر |
| DPM_LOT_EPSILON | 1e-8 | عتبة إسقاط الحصص |
| DPM_EST_FEE_BPS | 10 | رسوم تقديرية لكل أمر عند توليد الخطة، تدخل في cash_after |
| DPM_DEFAULT_SETTLEMENT_DAYS | 2 | افتراضي تسوية، يخصص لكل ورقة |
| DPM_BACKUP_DIR | ./data/backups | مجلد النسخ |
| DPM_DATA_PROVIDER | yahoo | مزود الحي: yahoo أو off |
| DPM_OPTIMIZER_ENABLED | false | Riskfolio فقط، لا علاقة له بمزود البيانات |
| DPM_CARRY_FORWARD_MAX | 5 | أقصى أيام تقويم لنقل إغلاق، بعدها يعتبر فجوة فعلية |

---

## 4. نموذج البيانات

المعرفات نص UUIDv4. التواريخ ISO-8601 بتوقيت UTC. كل عمود مالي TEXT بـ TypeDecorator عشري 8 منازل (قرار D9).

### 4.1 schema_version

جدول من صف واحد يحمل نسخة المخطط، تديره Alembic حصرا، ويعرضه /health. لا يدخل أحد القاعدة بتعديل يدوي.

### 4.2 clients

id PK، code UNIQUE، display_name، base_currency ISO (تحدد منازل العرض: AED دولار 2، KWD و BHD و OMR ثلاث خانات).

### 4.3 mandates

id PK، client_id FK، name، status (active أو closed). قيد: لا يفتح تفويض جديد لعميل له تفويض نشط إلا بقرار صريح من الواجهة.

### 4.4 accounts

id PK، account_code UNIQUE (مثل ACC-001، هو ما يدخل في CSV)، client_id FK، mandate_id FK **UNIQUE NOT NULL** (قرار D2: التفويض الواحد له حساب واحد)، currency، opening_cash.

### 4.5 mandate_versions

id PK، mandate_id FK، version INTEGER، effective_from، drift_tolerance، lot_method (fifo أو hifo)، benchmark_id NULL FK securities، weights_json. الأوزان تشمل CASH و مجموعها 1.0 بالضبط (قرار D8). لا تعدل نسخة قديمة أبدا.

### 4.6 securities

id PK، symbol UNIQUE، yahoo_ticker NULL (قد يختلف)، name، asset_class، currency، price_source (manual أو yahoo)، price_unit (share أو subunit مثل GBp و ZAc مع divisor)، settlement_days، manual_price NULL.

قاعدة الوحدة: كل سعر يدخل القاعدة يحول لعملة الورقة الرئيسة عند الاستيراد (GBp يقسم على 100)، ولا يخزن subunit أبدا.

### 4.7 transactions

id PK، account_id FK، external_ref NULL TEXT (مرجع كشف الوسيط)، trade_date، sequence INTEGER (ترتيب نفس اليوم، يخصص تلقائيا في نهاية اليوم إن لم يدخل)، side، security_id FK، quantity، unit_price، fee، fx_rate (إلى عملة الحساب)، status (active أو voided)، void_reason NULL، created_at.

- مفتاح idempotency: hash لكل الحقول السابقة عدا id و created_at (يشمل fx_rate و external_ref)
- التكرار المطابق: DuplicateTransactionError يعرض رقم الصفقة الأصلية. لا دمج صامت (قرار D7)
- الإلغاء: void يغير status فقط ويسجل السبب في audit. الحصص تعاد من الصفقات النشطة فقط. إلغاء صفقة شراء سابقة قد يجعل بيعا لاحقا فائضا، وهذا يظهر OverSellError وقت التقييم التالي برسالة تحدد الصفقتين، وهذه نتيجة مقصودة لا عطل

### 4.8 cash_flows

هذا جدول التدفقات النقدية، أساس رصيد النقد و TWR.

| العمود | النوع |
|---|---|
| id PK، account_id FK، external_ref NULL | |
| flow_date، sequence | نفس منطق الصفقات |
| type | deposit أو withdrawal أو dividend أو fee |
| security_id | NULL، إلزامي ل dividend |
| amount | عملة الحساب، موجب ل deposit و dividend، سالب ل withdrawal و fee |
| idempotency_key | UNIQUE، hash يشمل كل الحقول |

رصيد النقد في أي لحظة = opening_cash + مجموع flows + متحصلات الصفقات المنفذة النشطة (بيع ناقص شراء شامل الرسوم و fx). هذه هي المعادلة المرجعية الوحيدة، وتختبر golden.

التوزيعات تدخل من هنا (type=dividend مع idempotency)، ولا تدخل من corporate_actions إطلاقا.

### 4.9 corporate_actions

idempotent، type واحد فقط في v1: split. الأعمدة: id PK، security_id FK، action_date، ratio، idempotency_key UNIQUE.

### 4.10 price_history

سجل الأسعار التاريخية، من الاستيراد اليدوي (المصدر الأول) و من المزود (تعبئة اختيارية).

| العمود | النوع |
|---|---|
| security_id، price_date | PK مركب |
| close | عملة الورقة الرئيسة |
| source | manual أو yahoo أو openbb |
| fetched_at | |

قاعدة الاستيراد اليدوي: سعر manual بتاريخ لا تستبدله مزودات تلقائية أبدا، وتتمتع بالأسبقية دائما.

### 4.11 fx_history

| العمود | النوع |
|---|---|
| pair، as_of | PK مركب، pair بصيغة BASEQUOTE مثل AEDUSD معناها كمى USD مقابل 1 AED |
| rate | |
| source | manual أو yahoo |

قواعد التحويل بالترتيب: زوج مباشر، ثم عكس معكوس (1/rate)، ثم مثلثة عبر USD. أي تحويل يحتاج سعرا مفقودا يرفع FxRateMissingError بالزوج و التاريخ، ولا يقدر شيئا.

### 4.12 price_cache

السعر الحي فقط: security_id PK، price، fetched_at، source. ليس مصدرا للتقييم التاريخي أبدا، فقط للوحة اليومية و توليد الخطط.

### 4.13 rebalance_plans و rebalance_orders

plans: id PK، mandate_id FK، created_at، expires_at، status (draft أو confirmed أو executing أو executed أو expired أو infeasible)، snapshot_last_tx_id (آخر صفقة عند التوليد)، snapshot_last_flow_id، cash_after (بعد أوامر و رسوم تقديرية)، est_fee_bps المستخدمة.

orders: id PK، plan_id FK، security_id، side، quantity، estimated_price، reason، status (pending أو filled أو partial أو cancelled)، filled_quantity، filled_price، filled_fee.

### 4.14 audit_log

Append only بحماية مزدوجة (قرار D10): triggers SQL ترفض UPDATE و DELETE، وكل صف يحمل prev_hash و row_hash حيث row_hash = SHA-256 لكل الحقول السابقة مع prev_hash. أول صف prev_hash ثابت معروف.

الأعمدة: id PK، at، actor (ثابت local-operator)، entity_type، entity_id، action (create أو void أو import أو plan_execute أو backup أو restore)، payload_json، prev_hash، row_hash.

---

## 5. المحركات

### 5.1 محرك المحاسبة

- المصدر: الصفقات النشطة و الأحداث و التدفقات، مرتبة ب (التاريخ ثم sequence). الترتيب الكامل محدد دائما ولا يتساوى سطران
- BUY: lot جديد ب unit_cost = (unit_price + fee/quantity) مضروبا fx_rate، والنقد ينقص (quantity × unit_price + fee) × fx_rate
- SELL: يستهلك lots حسب lot_method لنسخة التفويض السارية (fifo الأقدم، hifo الأغلى)، realized = (unit_price ناقص unit_cost) × الكمية المستهلكة ناقص الرسوم، والنقد يزيد (quantity × unit_price ناقص fee) × fx_rate
- البيع الفائض: OverSellError، لا أثر جانبي
- Dust: كمية أقل من DPM_LOT_EPSILON تسقط من الحصص وتلحق بالـ realized
- Split: يضرب كمية الحصص المفتوحة قبل التاريخ بالـ ratio ويقسم التكلفة عليه
- المنازل: كميات 8، أسعار 4، نقد حسب عملة الحساب (2 أو 3)

### 5.2 محرك الأسعار و التقييم

ترتيب السعر لتاريخ D: آخر صف price_history بتاريخ أقل من أو يساوي D (manual أولوية عند تساوي التاريخ):

1. carry-forward: نقل إغلاق سابق مقبول (قرار D4) حتى DPM_CARRY_FORWARD_MAX أيام تقويم، ويوسم التقييم ب عدد أيام النقل
2. تجاوز الحد أو لا وجود أي إغلاق: ValuationGapError بالورقة و التاريخ
- السعر الحي (price_cache) للوحة اليومية و الخطة فقط، لا يدخل price_history آليا إلا بأمر تعبئة صريح
- المزودون خلف واجهة PriceProvider واحدة (get_quote و get_history)، yfinance تنفذها، OpenBB V5 اختياري، والتبديل من الإعدادات. لا يستدعي المزود إلا ورقة price_source=yahoo
- سعر تنفيذ الأوامر يتجاوز الكاش إجباريا عند التوليد، وسقف الانزلاق يضع علامة فقط

### 5.3 محرك الأداء

اتفاقية بداية اليوم مع مثال رقمي حاسم:

- قيمة يوم 1 عند الإغلاق = 100. إيداع 10 صباح يوم 2 (flow_date = يوم 2). إغلاق يوم 2 = 112.2
- عائد يوم 2 = 112.2 / (100 + 10) ناقص 1 = 2%
- القاعدة النصية: تدفق بتاريخ D يدخل قيمة بداية اليوم D، لا اليوم السابق ولا التالي

الخوارزمية:

1. نوافذ يومية من أول صفقة حتى تاريخ الطلب، التقييم بأسعار 5.2 و رصيد النقد من معادلة 4.8
2. عائد اليوم = (قيمة الإغلاق / قيمة بداية اليوم بعد إثبات تدفقاته) ناقص 1. أيام إجازة بأسعار منقولة تعطي صفرا وتتركب طبيعيا
3. TWR التراكمي = جداء عوائد الأيام. المتحصلات الداخلية (توزيع نقد) ليست تدفقا خارجيا ولا تكسر النافذة
4. فشل سعر يوقف بـ ValuationGapError و يحدد أول تاريخ فجوة، ولا يعود بأرقام جزئية
5. benchmark اختياري لكل نسخة: نفس النافذة و نفس آلية النقل و أسعار مضبوطة للـ splits

### 5.4 محرك الانحراف

- إجمالي = أسهم مقيومة بعملة الحساب (عبر 4.11) + النقد
- وزن كل عنصر بما فيه CASH = قيمته / الإجمالي
- الانحراف = الوزن الحالي ناقص وزن النسخة السارية، والمقارنة ضد drift_tolerance
- عنصر بلا هدف: يظهر بوزن هدف صفر و علامة unassigned

### 5.5 محرك إعادة التوازن

الخوارزمية، و الاستحالة تصرخ لا تختفي:

1. لقطة: الحصص، أسعار تقييم لكل ورقة (أي ورقة بلا سعر تفشل الخطة كلها ب ValuationGapError)، النقد الحالي، و نقد المتحصلات المتاحة حسب تواريخ التسوية (settlement_days لكل ورقة من يوم التنفيذ)
2. الأهداف من weights_json شامل CASH: قيمة هدف لكل عنصر = وزنه × الإجمالي
3. فروق تتحول أوامر، و أسقط ما دون DPM_DUST_VALUE
4. ترتيب: كل SELL أولا
5. النقد المتاح للشراء = النقد الحر + متحصلات بيع تستقر قبل أو يوم التنفيذ. الرسوم التقديرية = DPM_EST_FEE_BPS × قيمة كل أمر تدخل في الحساب
6. كميات الشراء تقرب لأسفل لسهم صحيح. إذا كان أقل من سهم يسقط
7. بينما النقد بعد الأوامر أقل من هدف CASH: قلص أكبر أمر شراء، فإذا استُنفد و النقد ما زال دون الهدف: ارجع خطوة و ارفع InfeasiblePlanError مع تقرير يشرح العجز بالأرقام. محفظة تبدأ بنقد دون الهدف تعلن الاستحالة من أول فحص ولا تعدلها خطة
8. الخطة تحفظ مع snapshot_last_tx_id و snapshot_last_flow_id و cash_after و expires_at

- التنفيذ: المدير ينفذ خارج النظام ثم يسجل كل أمر بحالته الفعلية (D1). fill يتحول صفقة نشطة ب external_ref = رقم الأمر، و partial يسجل الجزء و يترك الباقي، و cancelled يسجل السبب
- خطة مؤكدة يكتشف صفقات أو تدفقات أحدث من snapshot وقت التنفيذ: PlanStaleError و تطلب معاينة جديدة. لا تنفذ خطة فوق كتاب أحدث منها
- خطة منتهية الصلاحية: PlanExpiredError دائما

### 5.6 محول التحسين

مدخل: عوائد يومية من price_history لرموز النسخة، نافذة سنتين. مخرج: أوزان مقترحة HRP افتراضيا. الناتج proposal يعرض جانبيا فقط، و تحويله لنسخة جديدة قرار يدوي صريح. فشل التحسين معزول تماما عن بقية النظام.

---

## 6. واجهة REST

كل المبالغ في الردود نصوص (D9). الأخطاء بصيغة {"error": {"code": "...", "detail": "..."}}.

### 6.1 التهيئة (routes_bootstrap)

| Method | المسار | الوظيفة |
|---|---|---|
| GET / POST | /clients | قائمة ب قيم المحافظ / إنشاء |
| GET / POST | /mandates | تفاصيل تفويض / إنشاء |
| POST | /mandates/{id}/versions | إصدار نسخة أوزان جديدة (شامل CASH، مجموع 1.0) |
| GET / POST | /securities | قائمة / إنشاء ورقة بمصدرها و تسويتها و وحدتها |
| GET / POST | /accounts | حساب بكود نصي (قرار D2) |
| POST | /accounts/{id}/flows | تدفق نقدي (deposit أو withdrawal أو fee) |
| POST | /accounts/{id}/dividends | توزيع idempotent ب security و per-unit |
| POST | /fx | سعر صرف idempotent بزوج و تاريخ |
| POST | /prices/manual | سعر تاريخي يدوي، أولوية دائمة |

### 6.2 الدفتر (routes_book)

| Method | المسار | الوظيفة |
|---|---|---|
| POST | /transactions | صفقة idempotent (D7) |
| POST | /transactions/{id}/void | إلغاء بسبب مسجل |
| POST | /actions/splits | split idempotent |
| GET | /clients/{id}/positions | الحصص و النقد و القيمة و الربح |

### 6.3 الأداء و الخطط

| Method | المسار | الوظيفة |
|---|---|---|
| GET | /clients/{id}/performance | TWR مع أعمدة التقييم و أيام النقل |
| GET | /mandates/{id}/drift | جدول الانحراف |
| GET | /mandates/{id}/rebalance/preview | خطة دون حفظ |
| POST | /mandates/{id}/rebalance/confirm | حفظ مع expires_at |
| POST | /plans/{id}/orders/{oid}/fill | تسجيل تنفيذ فعلي filled أو partial |
| POST | /plans/{id}/orders/{oid}/cancel | إلغاء أمر بسبب |
| GET | /plans | كل الخطط و حالاتها |

### 6.4 الاستيراد و التصدير و التدقيق

| Method | المسار | الوظيفة |
|---|---|---|
| POST | /import/transactions?dry_run=true | جاف دائما افتراضيا، يعيد سطور القبول و الرفض و أسبابها، ولا يكتب شيئا إلا ب dry_run=false و عندئذ الكل أو لا شيء |
| POST | /import/prices?dry_run=true | أسعار تاريخية CSV |
| POST | /import/fx?dry_run=true | صرف CSV |
| POST | /import/flows?dry_run=true | تدفقات CSV |
| POST | /import/splits?dry_run=true | splits CSV |
| GET | /export/backup | نسخة كاملة ب manifest و SHA-256 |
| POST | /import/backup | فحص checksum ثم استبدال، يشترط توقف النشاط و إعادة تشغيل الخدمة |
| GET | /audit | السجل مع سلسلة hash |
| GET | /ui/{page} | صفحات HTML |
| GET | /health | حالة + schema_version |

مثال رد خطة (المبالغ نصوص):

```json
{
  "plan_id": "pl-...",
  "status": "confirmed",
  "expires_at": "2026-10-26T14:00:00Z",
  "cash_after": "21500.00",
  "cash_target": "20000.00",
  "est_fee_bps": "10",
  "orders": [
    {"order_id": "o-1", "symbol": "SPY", "side": "SELL",
     "quantity": "12", "estimated_price": "581.20",
     "reason": "overweight 7.4%", "status": "pending"}
  ]
}
```

---

## 7. الاستيراد

### 7.1 صيغ CSV

الترميز UTF-8، و BOM من Excel مقبول و يجرد قبل الفحص.

- صفقات: date, account_code, external_ref, side, symbol, quantity, unit_price, currency, fee, fx_rate, sequence
- أسعار تاريخية: date, symbol, close, currency
- صرف: date, pair, rate
- تدفقات: date, account_code, external_ref, type, amount, symbol
- splits: date, symbol, ratio

### 7.2 قواعد الاستيراد

- كل استيراد dry_run افتراضيا و يعرض كل شيء قبل الالتزام (قاعدة 8 في الاختبار)
- الالتزام كله أو لا شيء في معاملة واحدة، و الصف المرفوض لا يدخل أبدا و يبلغ برقمه و سببه
- account_code نصي لا UUID
- الاستيراد الناجح يسجل audit entry واحدة ت summarize عدد الصفوف لكل ملف

---

## 8. الأمان و التشغيل

- الربط على 127.0.0.1، و middleware يرفض أي طلب Host ليس 127.0.0.1 أو localhost، و أي طلب كتابة مع Origin من نطاق مختلف يرفض ب 403. هذا يغلق ثغرة CSRF المحلي من صفحات المتصفح المفتوحة
- DPM_LOCAL_TOKEN اختياري إضافي لكل عمليات الكتابة
- النسخ الاحتياطي عبر VACUUM INTO (آمن مع WAL بدون إيقاف)، و الاستعادة: فحص checksum، نسخة احتياطية تلقائية من الحالية، استبدال، ثم إعادة تشغيل إلزامية للخدمة قبل أي عملية
- سجل التدقيق من المرحلة الأولى و في كل مسار كتابة، و فحص سلامة سلسلة الـ hash endpoint يعرض أول كسر إن وجد

---

## 9. مصفوفة الاختبارات

### 9.1 لكل محرك ملف اختبار، و الحالات الدنيا

- محاسبة: شراء و بيع جزئي، بيع فائض يفشل و النقد لا يتغير، رسوم تدخل الطرفين، split و أثره التاريخي، إلغاء صفقة شراء قديمة ثم oversell لاحق يعلن بخطأ صريح، ترتيب نفس اليوم ب sequence، hifo يستهدف الأغلى
- أسعار: فشل مزود، carry-forward حتى الحد ثم ValuationGapError، أولوية manual على المزود بتاريخ متطابق، تجاوز الكاش عند التنفيذ و علامة الانزلاق
- أداء: المثال الرقمي في 5.3 ينتج 2% بالضبط، إيداع يوم 2 لا يدخل يوم 1، فجوة تعلن أول تاريخ، توزيع نقد داخلي لا يكسر النافذة، benchmark بنفس النقل
- انحراف: أوزان تشمل CADD؟ لا، تشمل CASH و مجموع القياس 1، عنصر بلا هدف موسوم، تفاوت النسخة فقط
- إعادة توازن: السيناريو الذهبي أدناه، محفظة تبدأ بنقد دون الهدف ترفع InfeasiblePlanError، dust لا يولد، كل SELL قبل كل BUY، رسوم تقديرية داخل cash_after، خطة منتهية ترفض، صفقة بعد snapshot ترفض ب PlanStaleError
- csv_io: dry_run لا يكتب شيئا، BOM يجرد، التزام كله أو لا شيء، account_code نصي
- backup: نسخة تستعاد و تعطي نفس الأرقام، checksum تالف يرفض قبل لمس القاعدة
- optimizer: عزل الفشل، proposal لا يلمس النسخ
- api: كل خطأ يعود بصيغة code و detail الموحدة
- security: Host دخيل يرفض، Origin دخيل على POST يرفض، token خاطئ يرفض
- audit: UPDATE و DELETE يرفضهما الـ trigger فعليا، سلسلة hash تكتشف أي تعديل تجريبي

### 9.2 السيناريوهات الذهبية (أرقام محسوبة يدويا مسبقا في fixtures، لا يخترعها المطور)

- **TWR**: المثال الكامل في 5.3 بقيم مطلقة 100 و 10 و 112.2 و عائد متوقع 0.02 بالضبط
- **رصيد النقد**: حساب opening_cash=1000، إيداع 500، شراء 10 سهم بسعر 50 و رسوم 5، بيع 4 بسهم بسعر 55 و رسوم 2، توزيع 0.5 لكل سهم على 6 أسهم، الرصيد المرجعي محسوب يدويا في الجدول المرفق داخل ملف الاختبار
- **إعادة توازن**: إجمالي 100,000، هدف نقد 5%، ورقة واحدة بوزن هدف 40% و فعلي 50%: أمر بيع محسوب بالضبط في الـ fixture، مع رسوم تقديرية 10bps و تقريب لأسفل
- **الاستحالة**: نقد فعلي 1% و هدف 5% و لا أوامر بيع متاحة: InfeasiblePlanError حتمي

### 9.3 التفاوتات

| القياس | التفاوت في الاختبارات التركيبية |
|---|---|
| كميات و حصص | 1e-8 |
| نقد و قيم | 0.01 |
| TWR داخلي golden | 1e-8 |
| TWR مقابل مصدر خارجي إن وجد | 1e-4 (نقطة أساس واحدة) |

---

## 10. خط الإنتاج CI

GitHub Actions على كل push و PR:

1. ruff check
2. mypy صارم على backend
3. pytest مع coverage، عتبة 90% لمحرك واحد على الأقل يكسر البناء لو هبط
4. pip-audit
5. pip-licenses يفحص الشجرة الكاملة المتعدية: أي حزمة AGPL أو GPL في الاعتماد المباشر أو المتعدى يفشل البناء
6. البناء الناجح شرط دمج مع branch protection على main

---

## 11. خطة التنفيذ

### المرحلة 1 (الأسبوعان 1 و 2): الدفتر و التقييم

المخطط كاملا بترحيل Alembic، محرك المحاسبة، التدفقات و رصيد النقد، استيراد CSV (جاف ثم التزام)، محرك الأسعار و التاريخ و carry-forward، و سجل التدقيق من أول صف. تعريف الانتهاء: CSV لعميل واحد يدخل و يخرج مركزا و رصيد نقد مطابقين للسيناريو الذهبي، و فجوة سعر تعلن خطأها.

### المرحلة 2 (الأسبوعان 3 و 4): الرؤية و الأداء

محرك TWR و الانحراف و لوحات dashboard و client و mandate، و تقرير الأداء بأيام النقل الموسومة، و النسخ الاحتياطي و الاستعادة. تعريف الانتهاء: تقرير أداء مطابق للسيناريو الذهبي و نسخة تستعاد على جهاز آخر.

### المرحلة 3 (الأسبوعان 5 و 6): الخطط و التحسين

محرك إعادة التوازن بكل قواعد القسم 5.5، دورة التنفيذ اليدوي ب حالات الأوامر، محول Riskfolio. تعريف الانتهاء: خطة صحيحة لتفويض متجاوز التفاوت، و سيناريو الاستحالة يفشل بالخطأ الصحيح.

---

## 12. قائمة قبول v1.0

- [ ] إقلاع بأمر واحد على جهاز نظيف دون إنترنت، و اللوحة تعمل بالأسعار اليدوية فقط
- [ ] كل ملفات الاختبار خضراء في CI مع عتبة التغطية
- [ ] محاولة UPDATE على audit_log عبر SQL ترفض فعليا
- [ ] طلب POST من Origin دخيل يرفض ب 403
- [ ] كل مبالغ JSON نصوص، و KWD يعرض ثلاث خانات
- [ ] dry_run افتراضي و التزام استيراد كامل أو فاشل كليا
- [ ] pip-licenses يمر بلا AGPL في الشجرة المتعدية
- [ ] README بخمسة أسطر للإقلاع

---

## 13. حدود النمو

المشروع يكتمل بالمراحل الثلاث و قائمة القبول. أي ميزة جديدة تقاس بسؤالين: هل يحتاجها مدير واحد على مكتبه، و هل تخص العميل النهائي؟ لو الثاني نعم فهي ملك ويلزجيت و لا تبنى هنا.

ما يجوز نقله مستقبلا لويلزجيت بلا مديونية قانونية: كل كود المشروع، و Riskfolio بترخيص BSD، و OpenBB V5 بترخيص Apache من مصدره الرسمي (إصدارات V4 وما قبلها AGPL و محرمة). الممنوع نقله: أي شيء من Ghostfolio أو Twenty أو أي أداة AGPL.

**نهاية المواصفة v1.1**
