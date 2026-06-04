# MSFS Texture Tool

本工具用于批量把 PNG 转为 MSFS 可用的 KTX2，并生成对应的 `.json` 和 `.FLAGS`。

## 启动

```bash
python3 "/Users/teory/Desktop/Fenix/KTX2/msfs_texture_tool/server.py"
```

打开：

```text
http://127.0.0.1:8787
```

## 已实现

- 拖拽或选择多个 PNG
- 自动识别后缀：`_ALBD`、`_COMP`、`_NORM`、`_EMIS`
- 调用 `basisu` 生成 KTX2
- 自动生成 mipmaps
- sRGB / Linear 自动或手动切换
- Fast / High Quality 质量档位
- 生成 `.json`
- 生成 `.FLAGS`

## 预设

- Albedo：UASTC，默认 sRGB
- Composite / ORM：UASTC，Linear，`NO_GAMMA_CORRECTION`
- Normal：UASTC，Linear，`normal_map`，`TANGENTDXT5N`
- Emissive：UASTC，默认 sRGB

## 关于“和官方 SDK 完全一致”

`basisu` 模式稳定、跨平台、适合批量生产 KTX2，但不能保证和 MSFS 官方 SDK
编译器输出 **字节完全一致**。官方 Package Tool 会按自身规则编译纹理，可能改变像素
数据、格式和 metadata。

如果目标是“官方 SDK 完全一致”，需要接入 MSFS SDK 的 Package Tool/Asset Builder。
当前界面保留了 `SDK Exact 模式` 入口；后续配置官方 SDK 路径后，应由该模式调用官方
工具链，而不是 `basisu`。
