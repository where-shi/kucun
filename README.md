# 鞋款补货 · 本地工作台

在本机按颜色和尺码查看 FBA 可售、已发在途、仓内货和销量，估算补货数量，再由使用者修改并确认。支持多批采购、取消登记、到货记录、参考货款、CSV 导出、本地备份和修改记录。

这是程序代码备份。`examples/demo-data.json` 中的名称、尺码、日期、库存与销量全部为虚构示例，不是任何店铺的真实资料。演示尺码对照不可用于真实采购。真实业务数据库和图片不在仓库内。

## 启动

需要 Python 3.10 或更新版本，无需额外安装 Python 依赖。Windows 中双击 `启动补货软件.cmd`，或在项目目录执行：

```text
python app/launcher.py
```

浏览器只是本地操作界面。程序只监听本机，数据保存在项目的 `data/` 目录。首次启动会载入虚构示例；以后启动保留本机已保存记录。独立运行服务可执行：

```text
python app/server.py serve --port 18765
```

## 日常使用

1. 在“按尺码补货”选款、选码，切换可售、在途、建议和方案视图。
2. 查看现货、预计需求及建议原因，填写自己的数量。0 表示明确不补；空白表示未填写。
3. 保存草稿。通过“方案详情”编辑参考采购价、查看整批预算或导出。
4. 实际下单后使用“我已采购，确认数量”登记；多批采购分别保留，可取消本地登记。
5. “更多”中可查看历史到货、数据来源、备份与修改记录。

补货建议按近期日均销量、预计运输时间与到仓后覆盖天数估算。系统在途与仓内货和现货分列计入计划；本地采购未核实反映到库存前，避免对同一缺口重复建议。估算供人工确认，不自动下单。

## 导入与助手接口

刷新页面只读取本地数据库，不会自行抓取领星。此代码备份不包含账号、外部同步脚本或每日定时任务。真实数据需要先在本地准备符合示例结构的完整 JSON 快照。首次使用真实数据时，另建数据库，以真实快照作为初始来源：

```text
python app/cli.py --db data/real.sqlite3 --seed 本地完整快照.json init
python app/launcher.py --db data/real.sqlite3 --runtime-file data/runtime-real.json
```

演示数据库与真实数据库分开保留；已经加载示例的数据库不能直接换成不含示例 SKU 的真实快照。之后在真实实例的“数据与记录”导入完整新快照，或执行：

```text
python app/cli.py --db data/real.sqlite3 backup
python app/cli.py --db data/real.sqlite3 import --json-file 本地完整快照.json
python app/cli.py --db data/real.sqlite3 status
python app/cli.py --db data/real.sqlite3 recommend --style 你的款号
```

已有数据库不会被默认演示数据替换。新快照必须包含全部既有 SKU；数量不会与旧快照叠加。修改方案等接口要求传入当前 revision，冲突时重新读取后合并，不能绕过版本检查。CLI 请求可使用 `--json-file` 提供 UTF-8 JSON，运行 `python app/cli.py --help` 查看全部接口。

每行快照可选填写 `orderMultiple`（每码建议数量倍数）、`minimumOrder`（款式参考起订量）、`cartonQuantity`（装箱双数）。全部为正整数；没有记录时按单双建议，起订/装箱要求显示待核。同款的款式级参数应保持一致。

领星货件对应仅接受本机 `data/imports/` 内的逐 SKU 已发凭据。使用前在本机环境变量 `REPLENISHMENT_LINGXING_SID` 设置自己的店铺 SID；未配置时货件匹配会拒绝执行。环境变量和凭据不提交 GitHub。该接口校验传入凭据及分配数量，本身不连接领星，也不会根据库存总在途猜测批次。

## 备份与恢复

“数据与记录”可创建数据库备份；备份保存在本机 `data/backups/`。GitHub 中的代码不能恢复库存、草稿、价格和采购记录。请将数据库备份和商品图另存到受保护的备份位置。

恢复前先关闭本软件服务，并另存当前完整 `data/` 目录；将选定 SQLite 备份作为新的数据库文件，使用 `python app/launcher.py --db 恢复目录/replenishment.sqlite3 --runtime-file 恢复目录/runtime.json` 打开核对。若需商品图及货件核对凭据，同时恢复对应 `product-images/` 和 `imports/` 子目录。核对无误前保留旧数据副本。

## 测试

```text
python -m unittest discover -s app -v
```

测试仅用临时数据库，覆盖完整快照、手改 0、采购确认和取消、分尺码价格、货件防重复分配及本机 HTTP 接口。安装 Node.js 后还会核对 JavaScript 与 Python 规则一致性；没有 Node.js 时该项会明确跳过。

## 目录

```text
app/                         本地服务、界面、CLI 与合成数据测试
examples/demo-data.json      完全虚构的可运行示例
recommendation-rules.js      可独立验证的基础补货规则
data/                        启动后生成的本地记录，禁止提交
```


## 开源许可

采用 MIT 许可，见 LICENSE。
