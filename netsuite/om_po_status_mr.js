/**
 * OM — PO status refresh (backfill + hourly safety net)
 * Map/Reduce. Sends every PO's current billing / payment status, totals and
 * quantity to Iron Bot in batches of 8, so the Lark Master PO List stays
 * filled even if a live push was missed (e.g. during an Iron Bot restart).
 * Rows not in the Master PO List are skipped by Iron Bot. Read-only in NetSuite.
 *
 * Deploy: Scheduled nightly 11 pm PT, Execute As Role = Administrator.
 *
 * @NApiVersion 2.1
 * @NScriptType MapReduceScript
 */
define(['N/query', 'N/log', './om_po_status_lib'], (query, log, lib) => {

  const URL = 'https://web-production-f965d.up.railway.app/integrations/netsuite/po-status';
  const SECRET = 'custsecretcustsecret_om_lark_card_push';
  const BATCH = 8;

  function getInputData() {
    const ids = query.runSuiteQL({
      query: "SELECT t.id FROM transaction t WHERE t.type = 'PurchOrd' ORDER BY t.id DESC"
    }).asMappedResults().map((r) => String(r.id));
    const batches = [];
    for (let i = 0; i < ids.length; i += BATCH) batches.push(ids.slice(i, i + BATCH));
    log.audit('PO status refresh', { pos: ids.length, batches: batches.length });
    return batches;
  }

  function map(ctx) {
    const ids = JSON.parse(ctx.value);
    lib.send(ids, URL, SECRET);
  }

  function summarize(summary) {
    summary.mapSummary.errors.iterator().each((key, err) => { log.error('Batch failed', err); return true; });
  }

  return { getInputData, map, summarize };
});
