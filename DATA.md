# 资料字典

CSV 为 UTF-8，第一行为列名。日期为北京时间，时间带偏移；资料可见时刻与业务发生时刻分别以字段含义为准。空值不能默认做零。

### `scales.csv`

报送时点之前不可见，修订仅替代同票指定旧版。净重区间用当票毛重、皮重和双向容差；`parcel` 是称重节点，同一节点可有独立观测。

字段：`ticket`、`rev`、`parcel`、`gross_kg`、`tare_kg`、`tolerance_kg`、`reported_at`、`replaces`。

### `movements.csv`

操作编号在多行共享；投入或拆分行共同描述一个操作。明确损耗有单独区间；某操作剩余未解释重量不是另一个收货节点。余货与拨出是互斥的实物去向，运输压差先不假定责任归属。

字段：`operation`、`kind`、`parent`、`child`、`observed_min_kg`、`observed_max_kg`、`occurred_at`。

### `origins.csv`

首道发货批的供货果园。

字段：`parcel`、`farm`。

### `waybills.csv`

运输记录的销售合同归属。

字段：`waybill`、`parcel`、`destination`、`buyer`、`contract`。

### `contracts.csv`

免赔量和上限均按运单累计。金额单位分，首次出运时的合同版本控制后续追补。

字段：`contract`、`version`、`active_from`、`deductible_kg`、`cents_per_kg`、`cap_cents`。

### `claims.csv`

申报量不是证明量；多次报送合计不等于可付重量。

字段：`notice`、`waybill`、`noticed_at`、`amount_kg`。

### `ledger.csv`

财务已经执行的历史分录，不要求所有旧决定在新资料下仍正确。关联证据以分号分隔；新调整用办理凭证区别重送。

字段：`entry`、`waybill`、`party`、`kind`、`cents`、`authorized_at`、`origin_refs`。

### `farm_credit.csv`

余额在同园同周共享，金额是已扣款之前的总可用额；不能挪用已关账时余量到后续周。

字段：`statement`、`farm`、`week`、`posted_at`、`gross_available_cents`、`closed_at`。

### `recoveries.csv`

历史扣抵与返还执行流水；旧抵扣 K21 可以是后来受争议的事实。返还到账后按承办单指定周视同抵销以前占量，入账状态替代待付。

字段：`entry`、`statement`、`direction`、`state`、`cents`、`known_at`、`related_waybill`、`original_entry`。
