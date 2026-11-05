/**
 * OM — PO live status to Lark
 * User event deployed on: Vendor Bill, Bill Payment (vendorpayment), Vendor Credit,
 * Item Receipt and Item Fulfillment. After any create / edit / delete / void,
 * finds the POs involved and sends their current billing / payment status to
 * Iron Bot (Master PO List row). Runs for every source: UI, Ramp sync, CSV.
 * Failures are logged and never block the save.
 *
 * Script parameters (same values as the PO script):
 *   custscript_om_po_status_url   https://<iron-bot-domain>/integrations/netsuite/po-status
 *   custscript_om_po_secret_id    API Secret id holding OM_CARD_PUSH_SECRET
 *
 * Deploy with: Execute As Role = Administrator, all execution contexts, status Released.
 *
 * @NApiVersion 2.1
 * @NScriptType UserEventScript
 */
define(['N/query', 'N/log', './om_po_status_lib'], (query, log, lib) => {

  const isId = (v) => /^[1-9][0-9]*$/.test(String(v || ''));

  function sublistValues(rec, sublistId, fieldId, onlyApplied) {
    const out = [];
    if (!rec) return out;
    try {
      const n = rec.getLineCount({ sublistId });
      for (let i = 0; i < n; i++) {
        if (onlyApplied && !rec.getSublistValue({ sublistId, fieldId: 'apply', line: i })) continue;
        const v = rec.getSublistValue({ sublistId, fieldId, line: i });
        if (isId(v)) out.push(String(v));
      }
    } catch (e) { /* sublist not on this record type */ }
    return out;
  }

  function posFromSalesOrders(soIds) {
    const ids = soIds.filter(isId);
    if (!ids.length) return [];
    return query.runSuiteQL({
      query: "SELECT DISTINCT tl.transaction po FROM transactionline tl JOIN transaction t ON t.id = tl.transaction " +
             "WHERE t.type = 'PurchOrd' AND tl.createdfrom IN (" + ids.map(() => '?').join(',') + ")",
      params: ids
    }).asMappedResults().map((r) => String(r.po));
  }

  function afterSubmit(ctx) {
    try {
      const recs = [ctx.newRecord, ctx.oldRecord].filter(Boolean);
      const type = (ctx.newRecord || ctx.oldRecord).type;
      const deleted = ctx.type === ctx.UserEventType.DELETE;
      const selfId = ctx.newRecord && !deleted ? String(ctx.newRecord.id) : '';
      let pos = [];

      if (type === 'vendorbill') {
        recs.forEach((r) => {
          pos = pos.concat(sublistValues(r, 'item', 'orderdoc'), sublistValues(r, 'expense', 'orderdoc'));
        });
        if (selfId) pos = pos.concat(lib.posLinkedTo([selfId]));
      } else if (type === 'vendorpayment') {
        let bills = [];
        recs.forEach((r) => { bills = bills.concat(sublistValues(r, 'apply', 'doc', true)); });
        pos = lib.posLinkedTo(bills);
      } else if (type === 'vendorcredit') {
        let bills = [];
        recs.forEach((r) => {
          const from = r.getValue('createdfrom');
          if (isId(from)) bills.push(String(from));
          bills = bills.concat(sublistValues(r, 'apply', 'doc', true));
        });
        pos = lib.posLinkedTo(bills);
      } else if (type === 'itemreceipt') {
        recs.forEach((r) => { const from = r.getValue('createdfrom'); if (isId(from)) pos.push(String(from)); });
      } else if (type === 'itemfulfillment') {
        // Drop-ship fulfillment: created from the SO; its POs change status.
        const sos = [];
        recs.forEach((r) => { const from = r.getValue('createdfrom'); if (isId(from)) sos.push(String(from)); });
        pos = posFromSalesOrders(sos);
        if (selfId) pos = pos.concat(lib.posLinkedTo([selfId]));
      }

      if (pos.length) lib.send(pos);
    } catch (e) {
      log.error('PO status trigger failed', e.message || String(e));
    }
  }

  return { afterSubmit };
});
