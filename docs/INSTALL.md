# 独立安装与验证

## 环境

Python 3.13（源码要求>=3.13,<3.14），Node.js 22.12+或24，npm，Docker（需要真实隔离数据库测试时），PostgreSQL 16、Redis 7。不要在真实学校服务目录运行测试。以下步骤不会调用模型，也不需要作者账号。

## 1. 后端独立依赖

在包根目录（Windows PowerShell）运行：

```powershell
python -m venv .venv
```

然后从包根目录安装实际后端包：

```powershell
.\.venv\Scripts\python.exe -m pip install "./apps/api[f2-assistant,dev]" --no-input
.\.venv\Scripts\python.exe -m pip check
```

Linux/macOS使用`.venv/bin/python`。依赖版本以`apps/api/pyproject.toml`为准；发布检查结果见VERIFICATION，不要通过随意降级解决无法下载的问题。

## 2. 前端

```powershell
cd apps/web
npm.cmd ci
npm.cmd run build:dingtalk-school-facts
npm.cmd run dev -- --host 127.0.0.1
```

Linux将`npm.cmd`换为`npm`。开发地址`http://127.0.0.1:5173/dingtalk-school-facts.html`；正式身份依赖自己的钉钉应用，普通浏览器没有登录态时不能绕过免登。前端构建成功不等于正式登录已接通。

## 3. 运行入口与角色分离

以下是完整私有配置准备好后的服务入口，不是默认演示账号。应激活虚拟环境，并从`apps/api`目录执行；也可从包根目录用`.venv`的Python运行`-m uvicorn`，增加`--app-dir apps/api`。

- 正式Product：`uvicorn app.composition.school_fact_live_product:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log --no-proxy-headers`
- Writer：`uvicorn app.composition.school_fact_product_writer:create_school_fact_product_writer_app --factory --host 127.0.0.1 --port 8010 --no-access-log --no-proxy-headers`
- Product私有配置路径环境变量：`SCHOOL_FACT_PRODUCT_CONFIG`。
- Writer私有配置路径环境变量：`SCHOOL_FACT_F1_WRITER_CONFIG_FILE`。
- 类型合同：`school_fact_live_settings.py`、`school_fact_product_writer.py`及其配置消费者。
- 前端的`VITE_DINGTALK_CORP_ID`和`VITE_DINGTALK_CLIENT_ID`只放本应用公开标识，App Secret只能在后端。

没有配置不能启动正式产品，不提供默认超级管理员或跳过身份的公开模式。禁止将数据库管理/治理口令交给Product。

## 4. 数据库和首次开通

参考`infra/docker/compose.school-facts-production.yml`及同目录环境模板，使用自己的全新卷、网络、角色和学校标识。Compose文件要求显式配置和已有本地镜像，不能直接把空示例当生产配置运行。

首次空库与既有实例升级不同：既有受控流程是合法角色引导→迁移至22→建立独立学校锚定及受信任首次身份→受控发布/确认→关闭初始化及治理→迁移至当前57→启动匹配Product/Reader/Writer。不能跳过学校锚定，不能拿`alembic upgrade head`代替完整建校，也不能降级到旧安全边界。

源码提供`app.composition.school_fact_live_initialization`、`scripts/school_facts_f1_init.py`和正式治理适配；需要实例自己的合法操作者、精确批准、确认及私有配置。**当前没有面向陌生部署者的一键交互式建校安装器。** 先用附带的隔离数据库验证脚本核对程序/迁移，再由部署者配置自己的真实接入；本包不冒充已经完成这一步。

## 5. 验证、停止与回退

独立验证说明/结果见VERIFICATION。仅在新建带专用标签的测试集群做合成初始化、读写、回执测试；不把测试授权用于真实实例。

回退只切回与当前schema兼容的代码/镜像，保留事实、权限撤销、账本和回执；不要重放正式动作，不把备份回滚当成普通应用回退。停止你新建的服务使用自己的Compose项目名，不运行`down -v`，不操作别人的容器。


## 可复验命令

安装完成后先执行（不创建数据库）：

```powershell
.\.venv\Scripts\python.exe scripts/verify_install.py
cd apps/api
..\..\.venv\Scripts\python.exe -m pytest
```

公开包的`apps/api/pytest.ini`只列安装相关、自包含的验证节点；未显式申请新测试库时，真实PG节点跳过并明确显示。其余历史测试源码作为参考保留，依赖私有实验/部署资产的节点未纳入公开默认测试集；不能把这个小集说成全仓回归。

要验证完整空库迁移、正式读写与撤权，回到包根目录，确保已取得`postgres:16`镜像后运行：

```powershell
docker pull postgres:16
.\.venv\Scripts\python.exe scripts/verify_install.py --run-disposable-database
```

助手只新建随机名称、任务标签、独立Docker网络和127.0.0.1随机端口的临时PG16；数据放tmpfs。完成或失败后按本次标签/ID核对再清理自己的容器及网络，并比较其他容器状态。不会连接已有学校数据库、不会调用模型。不运行同名恢复/迁移命令替代这项核验。

如进程被操作系统强制终止，可能无法执行finally；查看`runtime/install-verification/summary.json`中的本次资源名，确认标签后手动清理仅这些资源。不得为清理运行全局prune或删除其他项目卷。


## 合成示例

`fixtures/school_facts/`附带明确标注虚构学校的Excel、真值说明和清单。运行`python scripts/generate_simulated_boarding_school.py --check`只核对字节一致性，不写数据库。合成事实仍必须经自己的预检和正式确认，不要把旧示例中的学校标识和权限照搬到真实实例。

## 网络受限时

本次验证的精确Python版本清单在`constraints-tested-windows.txt`。有网络时可在安装命令中加`-c constraints-tested-windows.txt`以约束到本次已装版本。若已有从可信源下载的完整wheelhouse，可先使用自己的构建环境生成应用wheel，再用`pip install --no-index --find-links <wheelhouse> <application.whl>[f2-assistant,dev]`安装到全新环境；不要从来源不明的网站找打包密钥或可执行安装器。本包没有附带依赖二进制或作者的运行环境。
