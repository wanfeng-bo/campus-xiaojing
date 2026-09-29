# 第三方许可与素材来源

本项目Apache-2.0只覆盖有权公开的原始代码和设计，不替换第三方许可证。

- Python依赖及开发工具按本次全新验证环境的已安装发行包元数据核对；前端按package-lock.json及npm ci后的包核对。
- 完整逐包名称/版本/许可证/附带声明路径见[dependencies.json](third-party/dependencies.json)。下载到的依赖本体不随源码ZIP重分发。
- [licenses目录](third-party/licenses/)保留安装包提供的许可/版权声明。允许的多许可表达按原样保留，没有替第三方选掉义务。
- 包含MIT、Apache-2.0、BSD、ISC、MPL-2.0、LGPL-3.0-only、PSF等不同许可。尤其psycopg相关LGPL和certifi相关MPL声明保留；若以后分发容器或改动这些依赖，需要履行其具体许可证，而不能只带本项目LICENSE。
- PostgreSQL/Redis/Nginx/Python/Node等基础镜像没有打入源码包；获取和重分发镜像时适用其自身许可，不能认为源码Apache许可覆盖镜像全部组件。源码中Redis:7是历史浮动版本要求，不在本次声称所有Redis版本许可相同。
- 界面品牌和图标为仓库原生Vue/SVG组件；本包没有复制用户提供的参考截图、照片、下载图片、商业字体或第三方Logo资源。框架组件、钉钉SDK依赖仍按各自许可。
- 本包未承诺第三方永久支持或提供法律担保；自动元数据核对不等于完整法律意见。

Apache-2.0原文官方来源：https://www.apache.org/licenses/LICENSE-2.0 。本机下载失败时使用已安装pip所附同名标准许可证文本，核对版本及完整条款；没有改写许可证内容。

本次清单：Python 61项，npm 168项。
