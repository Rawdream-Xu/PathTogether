# JPEG 兼容请求与上传回退

请求 `fr_bf7e018623683717` 提供的原始文件为 1500 × 1200 三通道 baseline JPEG，2,101,189 字节。请求中的 `needs_conversion` 由工作台转换器关闭或初始化失败后的旧分流发出，不是 JPEG 解码器报错；请求没有附浏览器初始化异常，无法确定该用户发生了哪一种情况。

已有浏览器转换器和 native CLI 均成功处理该原件，生成 OME-TIFF 的 SHA-256 一致。服务端 `RasterSlide` 可直接读取原件，解码 RGB 与 Pillow 对原文件的整图解码逐像素一致。普通图片没有可用的组织物理标尺；服务端按既有像素数上限校验，默认 50 MP。

修复在本机转换器不可用的旧分流中，对服务端 capability 明确开放直传的 JPEG/BMP 回退到 `legacy-direct`；保留上传头校验、文件大小限制与服务端完整解码。SVS/KFB 等其他需要转换的文件仍走原有拦截。转换器可用时正常路径不变。

本机运行目录 `/mnt/ssd2/tests/jpeg-compat-20261105-lUHHwc` 为私有目录，包含用户样本，勿提交或公开。SSD2 NVMe 挂载、可写性已核验。146 项相关 JavaScript 测试通过；使用真实浏览器在隔离服务器阻断转换器加载并选择原始文件，验证创建原件 `legacy-direct` 任务，请求被测试拦截，无生产文件上传。隔离生产数据库副本验收、回滚探针与资源完整性验证通过。

2026-11-05（Asia/Shanghai）已发布 `localhost/pathtogether-demo:suite-20261105-jpeg-fallback`，实现提交 `ea88d6b667795f5605311a02e03f7aa9728234f9`。上线前后队列为空，备份和发布资料保存在 homepc `/home/solarise/releases/suite-20261105-jpeg-fallback`。正式域名的健康检查和静态资源校验通过。旧容器保留；核对队列后可执行该目录的 `deploy.py rollback` 回滚。

**2026-11-05 更新：该回退已撤销。** 直传原图不符合产品要求，线上已回滚到 suite-20261105c，本地由 808dc9e6 撤销。替代方案见 `docs/jpeg-convert-only-plan-20261105.md`：普通图片一律先在浏览器转换为 OME-TIFF 再上传，服务端拒收原图。
