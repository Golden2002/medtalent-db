# -*- coding: utf-8 -*-
"""
ops/tests/console_test.py —— 演示控制台端到端测试（需求 b2）

对每个演示页面发真实 HTTP 请求，断言的是**数据库状态**而不是页面文本：
  · 每个 GET 页面都能正常渲染（200 且含关键内容）
  · 现场新增维度 → 立刻出现在录入表单里，且 **DDL 次数为 0**
  · 表单提交 → 人才与选择结果真的入库
  · 交换包摄入 → 幂等；含身份标识的包被拒
  · 备份操作 → 可用
  · 清理 → 演示数据被清空

用法：python ops/tests/console_test.py
"""
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code", "demo"))
sys.path.insert(0, os.path.join(BASE, "code", "bridge"))
sys.path.insert(0, os.path.join(BASE, "code"))

import console as con  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
PORT = 8095
ROOT = "http://127.0.0.1:%d" % PORT
PASS, FAIL = H.PASS, H.FAIL
check, q1 = H.check, H.q1


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def db():
    return H.connect(con.DSN)


def get(path):
    try:
        with OPENER.open(ROOT + path, timeout=30) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def post(path, data):
    body = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
    req = urllib.request.Request(ROOT + path, data=body, method="POST")
    try:
        with OPENER.open(req, timeout=60) as r:
            return r.status, r.headers.get("Location", ""), r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location", ""), e.read().decode("utf-8")


def strip(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def main():
    print("=" * 78)
    print("演示控制台端到端测试（需求 b2）")
    print("=" * 78)

    with db() as c:
        con.reset_live(c)
        c.commit()
        con.add_dimension(c, "F_LIVE_A", "基层服务意愿",
                          ["非常愿意", "愿意", "犹豫", "不愿意"], False)
        con.add_dimension(c, "F_LIVE_B", "可接受的岗位族",
                          ["临床医疗", "药企医学事务", "CRO/临床研究"], True)
        c.commit()

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), con.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("【准备】演示控制台已起：%s" % ROOT)

    try:
        # -----------------------------------------------------------
        print("\n【T1】所有演示页面可正常渲染")
        pages = [("/", "建议的演示动线"), ("/jobs", "must"), ("/matrix", "重要性"),
                 ("/dimension", "现场新增一个维度"), ("/intake", "请选择"),
                 ("/match", "unknown"), ("/bridge", "memberKey"), ("/backup", "sha256")]
        for path, needle in pages:
            st, body = get(path)
            check(st == 200 and needle in body,
                  "GET %s → %d，含「%s」" % (path, st, needle))

        print("\n【T2】岗位图谱筛选与分页确实生效")
        st, b1 = get("/jobs?family=F02&page=1")
        st, b2 = get("/jobs?family=F01&page=1")
        check("F02" in b1 and "F01" in b2, "按族筛选返回不同内容")
        check(strip(b1) != strip(b2), "两个族的页面内容不同")

        print("\n【T3】能力矩阵按职业切换")
        st, bm = get("/matrix?occ=" + urllib.parse.quote("OCC-F02-01-01"))
        check(st == 200 and "能力" in bm, "能力矩阵可切换职业")
        check("必需" in bm or "加分" in bm, "显示了必需性标注")

        # -----------------------------------------------------------
        print("\n【T4】现场新增维度 → 立刻出现在录入表单，且零 DDL")
        with db() as c:
            before_tables = q1(c, "SELECT count(*) FROM information_schema.tables "
                                  "WHERE table_schema='mt' AND table_type='BASE TABLE'")
            before_cols = q1(c, "SELECT count(*) FROM information_schema.columns "
                                "WHERE table_schema='mt' AND table_name='person'")
        st, loc, _ = post("/dimension", {"title": "是否愿意值夜班",
                                         "options": "完全接受,可以商量,不接受", "multi": "0"})
        check(st == 303, "POST /dimension 返回 303")
        check("已新增维度" in urllib.parse.unquote(loc), "返回消息确认新增成功")

        st, body = get("/intake")
        check("是否愿意值夜班" in body, "刷新录入页后，新维度立刻出现在表单里")
        check("完全接受" in body, "n 个选项都渲染出来了")
        with db() as c:
            after_tables = q1(c, "SELECT count(*) FROM information_schema.tables "
                                 "WHERE table_schema='mt' AND table_type='BASE TABLE'")
            after_cols = q1(c, "SELECT count(*) FROM information_schema.columns "
                               "WHERE table_schema='mt' AND table_name='person'")
        check(after_tables == before_tables, "基础表数量未变（%d）" % before_tables)
        check(after_cols == before_cols, "person 列数未变（%d）→ 新增维度 DDL=0" % before_cols)

        # -----------------------------------------------------------
        print("\n【T5】表单提交 → 人才与选择结果真的入库")
        st, loc, _ = post("/intake", {"F_SUBJECT_CODE": "MT-LIVE-TEST",
                                      "F_LIVE_A": ["O1"], "F_LIVE_B": ["O1", "O2"]})
        check(st == 303, "POST /intake 返回 303")
        pid = urllib.parse.unquote(loc).split("：")[-1].strip()
        with db() as c:
            check(q1(c, "SELECT count(*) FROM person WHERE person_id=%s", (pid,)) == 1,
                  "人才实例已入库（%s）" % pid)
            check(q1(c, "SELECT value_code FROM field_value WHERE subject_id=%s "
                        "AND field_id='F_LIVE_A'", (pid,)) == "O1", "单选按 code 存储")
            check(q1(c, "SELECT value_codes FROM field_value WHERE subject_id=%s "
                        "AND field_id='F_LIVE_B'", (pid,)) == ["O1", "O2"], "多选按数组存储")
        st, body = get("/intake")
        check("MT-LIVE-TEST" in body, "录入页列出了刚提交的人才")

        # -----------------------------------------------------------
        print("\n【T6】匹配演示：三态与解释字段")
        with db() as c:
            with c.cursor() as cur:
                cur.execute("""INSERT INTO observation_window (window_id, person_id,
                                  window_type, start_date, coverage_note)
                               VALUES (%s,%s,'W1', CURRENT_DATE - 300, '演示窗口')
                               ON CONFLICT DO NOTHING""", ("ow_" + pid, pid))
            c.commit()
        st, body = get("/match?pid=" + urllib.parse.quote(pid))
        check(st == 200, "GET /match 返回 200")
        check("命中 met" in body and "缺口 gap" in body and "未知 unknown" in body,
              "页面同时展示 met / gap / unknown 三态")
        # 该人才没有已映射的能力 → 不应出现"具备"的误判
        with db() as c:
            check(q1(c, "SELECT count(*) FROM skill_assertion WHERE person_id=%s",
                     (pid,)) == 0, "该人才确实没有能力主张（因此不会有 met）")

        # -----------------------------------------------------------
        print("\n【T7】小程序交换包：示例可摄入、重复幂等、含身份标识被拒")
        with db() as c:
            ident_before = q1(c, "SELECT count(*) FROM external_identity")
        st, loc, _ = post("/bridge", {"use_example": "1"})
        m = urllib.parse.unquote(loc)
        check(st == 303 and "摄入成功" in m, "① 新示例包摄入成功：%s" % m[:70])
        with db() as c:
            ident_after_first = q1(c, "SELECT count(*) FROM external_identity")
        check(ident_after_first == ident_before + 1, "首次摄入新增 1 条外部身份绑定")
        st, loc2, _ = post("/bridge", {"again": "1"})
        check("幂等=True" in urllib.parse.unquote(loc2),
              "② 再投同一份包 → 幂等：%s" % urllib.parse.unquote(loc2)[:70])
        with db() as c:
            ident_after_second = q1(c, "SELECT count(*) FROM external_identity")
        check(ident_after_second == ident_after_first,
              "重复投递未新增身份绑定（仍为 %d）→ 没有产生第二个人" % ident_after_second)
        st, loc3, _ = post("/bridge", {"use_bad": "1"})
        check("INVALID_ARGUMENT" in urllib.parse.unquote(loc3),
              "③ 含 memberKey 的包被整包拒绝")
        with db() as c:
            check(q1(c, "SELECT count(*) FROM external_identity") == ident_after_second,
                  "被拒的包没有留下任何身份绑定")

        # -----------------------------------------------------------
        print("\n【T8】备份页可执行校验")
        st, loc4, _ = post("/backup", {"op": "verify"})
        check(st == 303 and ("校验通过" in urllib.parse.unquote(loc4)
                             or "校验发现问题" in urllib.parse.unquote(loc4)),
              "备份校验可执行：%s" % urllib.parse.unquote(loc4)[:40])

        # -----------------------------------------------------------
        print("\n【T9】清理演示数据")
        # 清理**必须覆盖本测试自己造的全部数据**，不只是 per_live_ 前缀。
        # 踩过的坑（问题 #40）：控制台的「小程序接入」演示走 exchange.ingest 造人，
        # 那些人的本地 ID 是 per_<sha1>，不在 per_live_ 前缀下——于是每跑一次
        # 全量回归就净增一个人，而 --reset 号称"一键恢复干净状态"。
        with db() as c:
            n_before_reset = q1(c, "SELECT count(*) FROM person")
        st, loc5, _ = post("/reset-live", {})
        check(st == 303 and "已清除演示数据" in urllib.parse.unquote(loc5), "清理命令成功")
        with db() as c:
            check(q1(c, "SELECT count(*) FROM person WHERE person_id LIKE %s",
                     (con.LIVE_PREFIX + "%",)) == 0, "演示人才已清空")
            check(q1(c, "SELECT count(*) FROM field_catalog WHERE field_id LIKE %s",
                     ("F_LIVE%",)) == 0, "演示维度已清空")
            check(q1(c, "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema='mt' AND table_type='BASE TABLE'") == before_tables,
                  "始终基础表数量未变（%d）" % before_tables)
            # 关键：bridge 演示造出的人也必须被清掉
            check(q1(c, """SELECT count(*) FROM external_identity
                            WHERE external_person_id LIKE 'wx_demo_%'""") == 0,
                  "bridge 演示的外部身份绑定已清空（不残留 wx_demo_ 前缀）")
            n_after_reset = q1(c, "SELECT count(*) FROM person")
            check(n_after_reset <= n_before_reset,
                  "清理后人数未增加（%d → %d）——演示链路不留人"
                  % (n_before_reset, n_after_reset))
    finally:
        srv.shutdown()

    return H.report()


if __name__ == "__main__":
    sys.exit(main())
