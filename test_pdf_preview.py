import json
import fitz
import pdf_preview


def _pdf(pages):
    doc = fitz.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"SO6926 page {i+1}")
    return doc.tobytes()


class FakeLark:
    def __init__(self, pdf):
        self.pdf, self.uploads, self.posts = pdf, [], []
    def get_message_resource(self, mid, key, kind):
        assert kind == "file"
        return self.pdf
    def upload_image(self, b):
        assert b[:4] == b"\x89PNG"
        self.uploads.append(b)
        return f"img_{len(self.uploads)}"
    def _post(self, path, body=None):
        self.posts.append((path, body))
        return {"code": 0}


def _msg(name="SO6926.pdf", mtype="file"):
    return {"message_type": mtype, "message_id": "om_1", "chat_id": "oc_po",
            "content": json.dumps({"file_key": "file_x", "file_name": name})}


def test_filters():
    assert pdf_preview.should_preview(_msg(), "ou_hannah", "ou_bot")
    assert not pdf_preview.should_preview(_msg("art.ai"), "ou_hannah", "ou_bot")
    assert not pdf_preview.should_preview(_msg(), "ou_bot", "ou_bot")
    assert not pdf_preview.should_preview(_msg(mtype="text"), "ou_hannah", "ou_bot")


def test_reply_card_caps_pages():
    lark = FakeLark(_pdf(5))
    pdf_preview.handle_pdf_message(lark, _msg())
    assert len(lark.uploads) == pdf_preview.MAX_PAGES == 3
    path, body = lark.posts[0]
    assert path == "/open-apis/im/v1/messages/om_1/reply"
    card = json.loads(body["content"])
    assert [e["img_key"] for e in card["elements"] if e["tag"] == "img"] == ["img_1", "img_2", "img_3"]
    assert "of 5" in card["elements"][-1]["elements"][0]["content"]


def test_single_page():
    lark = FakeLark(_pdf(1))
    pdf_preview.handle_pdf_message(lark, _msg())
    card = json.loads(lark.posts[0][1]["content"])
    assert card["elements"][-1]["elements"][0]["content"] == "Page 1 of 1"


def test_bad_pdf_does_not_raise():
    lark = FakeLark(b"not a pdf")
    pdf_preview.handle_pdf_message(lark, _msg())
    assert lark.posts == []


def test_chat_report_and_join():
    def user_get(path, params):
        return {"code": 0, "data": {"items": [{"chat_id": "a", "name": "PO"}, {"chat_id": "b", "name": "Quotes"}]}}
    def bot_get(path, params):
        return {"code": 0, "data": {"items": [{"chat_id": "a", "name": "PO"}]}}
    rows = pdf_preview.chat_report(user_get, bot_get)
    assert [(r["name"], r["iron_bot_in"]) for r in rows] == [("Quotes", False), ("PO", True)]
    calls = []
    res = pdf_preview.join_all(lambda p, b, q: calls.append((p, b, q)) or {"code": 0}, rows, "cli_x")
    assert calls == [("/open-apis/im/v1/chats/b/members", {"id_list": ["cli_x"]}, {"member_id_type": "app_id"})]
    assert res[0]["added"]


def test_backfill_skips_done_and_bot_files():
    pdf = _pdf(1)
    def msg(mid, name, sender="ou_h", parent=None, t="file"):
        return {"message_id": mid, "msg_type": t, "parent_id": parent, "sender": {"id": sender},
                "body": {"content": json.dumps({"file_key": "k", "file_name": name})}}
    hist = [msg("m1", "SO1.pdf"), msg("m2", "PO2.pdf"), msg("m3", "x.ai"),
            msg("m4", "bot.pdf", sender="cli_x"), msg("r", "", sender="cli_x", parent="m2", t="interactive")]
    lark = FakeLark(pdf)
    user_get = lambda p, q: {"code": 0, "data": {"items": hist}}
    bot_get = lambda p, q: {"code": 0, "data": {"items": [{"chat_id": "c", "name": "PO"}]}}
    done = []
    s = pdf_preview.backfill(lark, user_get, bot_get, "cli_x", is_done=lambda m: False, mark_done=done.append)
    assert (s["pdfs"], s["already"], s["posted"]) == (2, 1, 1)
    assert done == ["m1"] and lark.posts[0][1]["reply_in_thread"] is True
