/**
 * OM — PO live status (shared helpers)
 * Works out a PO's billing / payment status and sends it to Iron Bot, which
 * writes it onto the PO's Master PO List row in Lark. Read-only in NetSuite.
 *
 * @NApiVersion 2.1
 * @NModuleScope Public
 */
define(['N/query', 'N/https', 'N/crypto', 'N/encode', 'N/runtime', 'N/log'],
  (query, https, crypto, encode, runtime, log) => {

    // NetSuite PO status code -> Lark "Billing Status" option.
    const BILLING = {
      A: 'Not Approved', B: 'Not Received', C: 'Rejected', D: 'Partially Received',
      E: 'Partially Received – Not Billed', F: 'Received – Not Billed', G: 'Billed', H: 'Closed'
    };
    const round = (n) => Math.round((Number(n) || 0) * 100) / 100;

    function status(poId) {
      const head = query.runSuiteQL({
        query: "SELECT t.status code, BUILTIN.DF(t.status) label, t.foreigntotal total FROM transaction t WHERE t.id = ? AND t.type = 'PurchOrd'",
        params: [poId]
      }).asMappedResults()[0];
      if (!head) return null;

      // Bill lines that came from this PO, with each bill's paid share.
      const bills = query.runSuiteQL({
        query: "SELECT b.id, b.foreigntotal total, b.foreignamountpaid paid, SUM(ABS(bl.foreignamount)) fromthis " +
               "FROM previoustransactionlinelink l " +
               "JOIN transaction b ON b.id = l.nextdoc " +
               "JOIN transactionline bl ON bl.transaction = l.nextdoc AND bl.id = l.nextline " +
               "WHERE l.previousdoc = ? AND b.type = 'VendBill' " +
               "GROUP BY b.id, b.foreigntotal, b.foreignamountpaid",
        params: [poId]
      }).asMappedResults();

      let billed = 0, paid = 0;
      bills.forEach((b) => {
        const part = Number(b.fromthis) || 0;
        const total = Math.abs(Number(b.total) || 0);
        billed += part;
        if (total > 0) paid += part * Math.min(1, Math.abs(Number(b.paid) || 0) / total);
      });
      billed = round(billed); paid = round(paid);

      let billing = BILLING[head.code] || '';
      if (billed > 0 && ['B', 'D', 'E', 'F'].includes(head.code)) billing = 'Partially Billed';
      let payment = 'Unpaid';
      if (paid > 0) payment = (billed > 0 && paid >= billed - 0.01) ? 'Paid' : 'Partially Paid';

      return {
        poId: String(poId),
        nsStatus: String(head.label || '').replace(/^Purchase Order\s*:\s*/, ''),
        billingStatus: billing, paymentStatus: payment,
        total: round(head.total), billed, paid
      };
    }

    function sign(body, secretId) {
      const ts = String(Math.floor(Date.now() / 1000));
      const hmac = crypto.createHmac({
        algorithm: crypto.HashAlg.SHA256,
        key: crypto.createSecretKey({ secret: secretId, encoding: encode.Encoding.UTF_8 })
      });
      hmac.update({ input: ts + '.' + body, inputEncoding: encode.Encoding.UTF_8 });
      return { 'Content-Type': 'application/json', 'X-OM-Timestamp': ts, 'X-OM-Signature': hmac.digest({ outputEncoding: encode.Encoding.HEX }) };
    }

    /** Send the current status of these POs to Iron Bot. Never throws.
     *  url / secretId come from the calling script's own parameters. */
    function send(poIds, url, secretId) {
      try {
        const ids = Array.from(new Set((poIds || []).map(String).filter((id) => /^[1-9][0-9]*$/.test(id)))).slice(0, 50);
        if (!ids.length) return;
        const pos = ids.map(status).filter(Boolean);
        if (!pos.length) return;
        const body = JSON.stringify({ version: 1, account: runtime.accountId, pos });
        const res = https.post({ url, body, headers: sign(body, secretId) });
        log.audit('PO status to Lark', { pos: ids.join(','), code: res.code, body: (res.body || '').slice(0, 300) });
      } catch (e) {
        log.error('PO status to Lark failed', e.message || String(e));
      }
    }

    /** POs linked to any of these transactions (bills, receipts, fulfillments). */
    function posLinkedTo(tranIds) {
      const ids = (tranIds || []).map(String).filter((id) => /^[1-9][0-9]*$/.test(id));
      if (!ids.length) return [];
      return query.runSuiteQL({
        query: "SELECT DISTINCT l.previousdoc po FROM previoustransactionlinelink l JOIN transaction p ON p.id = l.previousdoc " +
               "WHERE p.type = 'PurchOrd' AND l.nextdoc IN (" + ids.map(() => '?').join(',') + ")",
        params: ids
      }).asMappedResults().map((r) => String(r.po));
    }

    return { status, send, posLinkedTo, sign };
  });
