import unittest

from netsuite_billing_sync import plan_updates, field_text, STATUS_MAP


class PlanUpdatesTest(unittest.TestCase):
    def rec(self, rid, ns_id, billing=None, raw=None, synced=None):
        f = {"NetSuite PO ID": ns_id}
        if billing is not None:
            f["Billing Status"] = billing
        if raw is not None:
            f["NetSuite PO Status"] = [{"text": raw, "type": "text"}]
        if synced is not None:
            f["Billing Last Synced"] = synced
        return {"record_id": rid, "fields": f}

    def test_changed_rows_only(self):
        ns = {"1": "G", "2": "F", "3": "B"}
        records = [
            self.rec("r1", "1", "Billed", "Fully Billed", 123),        # unchanged
            self.rec("r2", "2", "Not Received", "Pending Receipt", 1),  # changed
            self.rec("r3", [{"text": "3", "type": "text"}]),            # never synced
            self.rec("r4", "999"),                                      # not in NetSuite
            self.rec("r5", ""),                                         # no id
        ]
        updates, stats = plan_updates(ns, records, 555)
        self.assertEqual([u["record_id"] for u in updates], ["r2", "r3"])
        self.assertEqual(updates[0]["fields"], {"Billing Status": "Received – Not Billed",
                                                "NetSuite PO Status": "Pending Bill",
                                                "Billing Last Synced": 555})
        self.assertEqual(stats["unchanged"], 1)
        self.assertEqual(stats["not_in_netsuite"], 1)
        self.assertEqual(stats["no_ns_id"], 1)

    def test_every_code_maps_to_lark_option(self):
        options = {"Not Approved", "Not Received", "Partially Received",
                   "Partially Received – Not Billed", "Received – Not Billed",
                   "Billed", "Closed", "Rejected"}
        self.assertTrue({b for _, b in STATUS_MAP.values()} <= options)

    def test_field_text(self):
        self.assertEqual(field_text([{"text": "PO", "type": "text"}, {"text": "1"}]), "PO1")
        self.assertEqual(field_text(None), "")
        self.assertEqual(field_text("x "), "x")


if __name__ == "__main__":
    unittest.main()
