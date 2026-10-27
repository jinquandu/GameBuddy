"""对外版本与内部构建号。

用户指示(2026-10-26):**对外版本恒定 V0.1**——界面/EXE 属性一律显示
V0.1,不给用户「有多个版本」的误解;迭代用内部构建号 BUILD 区分
(YYYYMMDD-rN[a-z] 单调递增),只在「复制诊断信息」里出现,供排障对版本。

自动更新(REQ-20261026-002)的比较口径:VERSION 恒 0.1 视为相等,实际
先后由 BUILD 决定(auto_update.build_tuple)。发布清单 latest.version
也固定填 "0.1",build 填真实构建号。
"""
VERSION = "0.1"          # 对外恒定,不改
BUILD = "20261027-r12"   # 内部构建号:每次出包递增
PRODUCT = "陪玩大师姐"
VENDOR = "大师姐"


def display():
    """用户可见版本(界面/托盘)。"""
    return "V0.1"


def label():
    """诊断信息用:含内部构建号(客户看不到,发给客服对版本)。"""
    return f"{PRODUCT} {display()}（构建 {BUILD}）"
