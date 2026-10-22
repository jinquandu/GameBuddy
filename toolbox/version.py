"""构建版本号(REQ-20261022-009):界面可见版本与构建号。

每次出包前更新 BUILD(包名后缀对齐,如 20261022-r7)。VERSION 用语义版本,
对外正式名 GameBuddy-vX.Y 与之对应;候选包可同内容不同名,但必须在
PACKAGING.md 登记源码提交与哈希映射。
"""
VERSION = "0.2.0"
BUILD = "20261022-r7"
PRODUCT = "陪玩大师姐"
VENDOR = "大师姐"


def label():
    return f"{PRODUCT} v{VERSION} · 构建 {BUILD}"
