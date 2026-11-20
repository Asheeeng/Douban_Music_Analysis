"""回归测试使用本地 HTML 和构造响应，不额外请求网站。"""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd
import requests

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("douban", PROJECT / "douban_music_analysis.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def page(start=0, indices=(0,), metadata="歌手 / 2004/04/26 / 专辑 / CD / 放克/灵歌/R&amp;B"):
    rows = []
    for index in indices:
        album_id = str(1000 + index)
        rows.append(f'''<tr class="item" id="{album_id}">
          <td><a class="nbg" href="/subject/{album_id}/" title="歌手 - A &amp; B"
            onclick="moreurl(this,{{subject_id:'{album_id}',i:'{index}'}})">封面</a></td>
          <td><div class="pl2"><a href="/subject/{album_id}/">A &amp; B<span>别名</span></a>
            <p class="pl">{metadata}</p><span class="rating_nums">9.2</span>
            <span>12,345人评价</span></div></td></tr>''')
    return (f'<html><h1>豆瓣音乐 Top 250</h1><div class="article"><table>{"".join(rows)}</table></div>'
            f'<div class="paginator"><span class="thispage">{start//25+1}</span></div>'
            '<div id="footer">页尾</div></html>')


def response(html, start=0, status=200):
    result = Mock()
    result.status_code = status
    result.content = html.encode("utf-8")
    result.url = f"{app.BASE_URL}?start={start}"
    result.headers = {"Content-Type": "text/html; charset=utf-8"}
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}")
    return result


class ParserTests(unittest.TestCase):
    def test_saved_html_has_real_gaps(self):
        for start, missing in ((75, 82), (100, 111), (125, 133)):
            html = (PROJECT / "output" / "debug_pages" / f"page_start_{start}.html").read_text()
            records, diagnostic = app.parse_music_page(html, start, "测试")
            self.assertEqual(len(records), 24)
            self.assertEqual(diagnostic["有效专辑链接条目数"], 24)
            self.assertEqual(diagnostic["网页缺失名次"], [missing])
            self.assertEqual(records[-1]["榜单名次"], start + 25)

    def test_html_entities_aliases_and_slashes(self):
        records, _ = app.parse_music_page(page(), 0, "测试")
        record = records[0]
        self.assertEqual(record["专辑名称"], "A & B")
        self.assertEqual(record["专辑别名"], "别名")
        self.assertEqual(record["发行日期"], "2004/04/26")
        self.assertEqual(record["音乐流派"], "放克/灵歌/R&B")
        self.assertEqual(record["评价人数"], 12345)

    def test_missing_type_does_not_shift_medium(self):
        fields, notes = app.parse_metadata("李志 / 2007-01-11 / CD / 民谣")
        self.assertEqual((fields["唱片类型"], fields["载体"], fields["音乐流派"]), ("", "CD", "民谣"))
        self.assertTrue(notes)

    def test_multiple_artists_and_missing_type(self):
        fields, _ = app.parse_metadata("AC / DC / 2004/04/26 / CD / 摇滚")
        self.assertEqual(fields["歌手"], "AC / DC")
        self.assertEqual(fields["唱片类型"], "")
        self.assertEqual(fields["载体"], "CD")

    def test_explicit_empty_field_keeps_positions(self):
        fields, _ = app.parse_metadata("歌手 / 2004 /  / CD / 民谣")
        self.assertEqual(fields["唱片类型"], "")
        self.assertEqual(fields["载体"], "CD")

    def test_multiple_type_labels_keep_medium_and_genre(self):
        fields, notes = app.parse_metadata("Original Soundtrack / 2009-03-30 / Import / Soundtrack / CD / 原声")
        self.assertEqual(fields["唱片类型"], "Import / Soundtrack")
        self.assertEqual(fields["载体"], "CD")
        self.assertEqual(fields["音乐流派"], "原声")
        self.assertEqual(notes, [])

    def test_missing_fields_do_not_drop_record(self):
        html = page(metadata="").replace('<span class="rating_nums">9.2</span>', "")
        raw = pd.DataFrame(app.parse_music_page(html, 0, "测试")[0], columns=app.COLUMNS)
        clean = app.clean_data(raw)
        self.assertEqual(len(clean), 1)
        self.assertEqual(clean.iloc[0]["歌手"], "未知")
        self.assertTrue(pd.isna(clean.iloc[0]["豆瓣评分"]))

    def test_missing_rank_is_failure(self):
        with self.assertRaisesRegex(ValueError, "排名缺失"):
            app.parse_music_page(page().replace("i:'0'", "other:'0'"), 0, "测试")

    def test_missing_link_is_failure(self):
        with self.assertRaisesRegex(ValueError, "没有有效专辑链接"):
            app.parse_music_page(page().replace("/subject/1000/", "/invalid/"), 0, "测试")

    def test_captcha_truncation_and_wrong_page_are_failures(self):
        for html in ("<html>请完成验证码</html>", page().replace("</html>", ""), page(start=25, indices=(25,))):
            with self.subTest(html=html[:30]), self.assertRaises(ValueError):
                app.parse_music_page(html, 0, "测试")

    def test_conflicting_cover_and_title_indices_are_failure(self):
        html = page().replace('href="/subject/1000/">A', 'href="/subject/1000/" onclick="x({i:1})">A')
        with self.assertRaisesRegex(ValueError, "互相冲突"):
            app.parse_music_page(html, 0, "测试")

    def test_same_name_different_ids_are_kept(self):
        raw = pd.DataFrame(app.parse_music_page(page(indices=(0, 1)), 0, "测试")[0], columns=app.COLUMNS)
        self.assertEqual(len(app.clean_data(raw)), 2)
        repeated = pd.concat([raw, raw.iloc[[0]]], ignore_index=True)
        self.assertEqual(len(app.clean_data(repeated)), 2)

    def test_conflicting_duplicate_rank_or_id_is_failure(self):
        raw = pd.DataFrame(app.parse_music_page(page(indices=(0, 1)), 0, "测试")[0], columns=app.COLUMNS)
        for column in ("榜单名次", "专辑ID"):
            bad = raw.copy()
            bad.loc[1, column] = bad.loc[0, column]
            with self.subTest(column=column), self.assertRaisesRegex(ValueError, "冲突重复"):
                app.clean_data(bad)


class FailureProtectionTests(unittest.TestCase):
    def test_second_page_timeout_preserves_all_previous_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "output"
            output.mkdir()
            sentinel = output / "原有数据.csv"
            sentinel.write_bytes(b"previous valid data")
            session = Mock()
            session.get.side_effect = [response(page()), requests.Timeout("模拟读取超时"),
                                       requests.Timeout("模拟读取超时")]
            with patch.object(app.requests, "Session") as factory, patch.object(app.time, "sleep"):
                factory.return_value.__enter__.return_value = session
                with self.assertRaisesRegex(RuntimeError, "start=25 请求失败.*模拟读取超时"):
                    app.run_pipeline(output)
            self.assertEqual(sentinel.read_bytes(), b"previous valid data")
            self.assertEqual(len(list(output.iterdir())), 1)
            self.assertEqual(session.get.call_count, 3)

    def test_transient_connection_failure_retries_only_once(self):
        session = Mock()
        valid = response(page())
        session.get.side_effect = [requests.exceptions.SSLError('UNEXPECTED_EOF_WHILE_READING'), valid]
        with patch.object(app.time, "sleep") as sleep:
            self.assertIs(app.request_page(session, 0), valid)
            sleep.assert_called_once_with(app.RETRY_DELAY)
        self.assertEqual(session.get.call_count, 2)

    def test_certificate_failure_is_not_retried(self):
        session = Mock()
        session.get.side_effect = requests.exceptions.SSLError('CERTIFICATE_VERIFY_FAILED')
        with self.assertRaisesRegex(RuntimeError, 'CERTIFICATE_VERIFY_FAILED'):
            app.request_page(session, 0)
        self.assertEqual(session.get.call_count, 1)

    def test_http_restriction_stops_without_retry(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(app.requests, "Session") as factory:
            session = factory.return_value.__enter__.return_value
            session.get.return_value = response("拒绝访问", status=429)
            with self.assertRaisesRegex(RuntimeError, "限制访问.*429"):
                app.crawl_all_pages(Path(temp))
            self.assertEqual(session.get.call_count, 1)

    def test_chart_failure_preserves_previous_output(self):
        raw = pd.DataFrame(app.parse_music_page(page(), 0, "测试")[0], columns=app.COLUMNS)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "output"
            output.mkdir()
            sentinel = output / "原有图表.png"
            sentinel.write_bytes(b"previous chart")
            with patch.object(app, "crawl_all_pages", return_value=(raw, [])), \
                    patch.object(app, "visualize", side_effect=OSError("模拟保存图表失败")):
                with self.assertRaisesRegex(OSError, "模拟保存图表失败"):
                    app.run_pipeline(output)
            self.assertEqual(sentinel.read_bytes(), b"previous chart")

    def test_publish_rename_failure_restores_output(self):
        with tempfile.TemporaryDirectory() as temp:
            output, stage = Path(temp) / "output", Path(temp) / "stage"
            output.mkdir()
            (output / "data.csv").write_bytes(b"old")
            stage.mkdir()
            original_rename = Path.rename

            def rename(path, target):
                if path == stage:
                    raise OSError("模拟目录替换失败")
                return original_rename(path, target)

            with patch.object(Path, "rename", rename), self.assertRaises(OSError):
                app.publish_outputs(stage, output)
            self.assertEqual((output / "data.csv").read_bytes(), b"old")


if __name__ == "__main__":
    unittest.main()
