/**
 * OM — PO to Master PO List (instant)
 * User event on Purchase Order, afterSubmit (create + edit).
 * Sends the PO header + product lines to Iron Bot: Master PO List row + one PO Lines row per item.
 * Header only: no production cards are created. Failures never block saving the PO.
 *
 * Script parameters:
 *   custscript_om_po_ironbot_url  e.g. https://<iron-bot-domain>/integrations/netsuite/po
 *   custscript_om_po_secret_id    API Secret id holding OM_CARD_PUSH_SECRET (same key as Send to Lark)
 *
 * @NApiVersion 2.1
 * @NScriptType UserEventScript
 */
define(['N/https', 'N/crypto', 'N/encode', 'N/runtime', 'N/search', 'N/log'],
  (https, crypto, encode, runtime, search, log) => {

    const text = (v) => (v === null || v === undefined) ? '' : String(v).trim();

    function afterSubmit(ctx) {
      if (![ctx.UserEventType.CREATE, ctx.UserEventType.EDIT, ctx.UserEventType.XEDIT, ctx.UserEventType.APPROVE]
          .includes(ctx.type)) return;
      try {
        const po = ctx.newRecord;
        const poId = String(po.id);
        let poNumber = text(po.getValue('tranid'));
        if (!poNumber) poNumber = text(search.lookupFields({ type: 'purchaseorder', id: poId, columns: ['tranid'] }).tranid);

        let soNumber = '', customer = '', soShip = '';
        const soId = po.getValue('createdfrom');
        if (soId) {
          const so = search.lookupFields({ type: 'salesorder', id: soId, columns: ['tranid', 'entity', 'shipaddress'] });
          soNumber = text(so.tranid);
          customer = (so.entity && so.entity[0]) ? text(so.entity[0].text).replace(/^\d+\s+/, '') : '';
          soShip = text(so.shipaddress);
        }
        const poShip = text(po.getValue('shipaddress'));
        // A PO address counts only if it has a street line (more than name + country).
        const shipTo = (poShip.split('\n').filter(Boolean).length >= 3) ? poShip : soShip;

        // Product lines only (no shipping, fees, setup/mold, tax or discount lines).
        const SKIP_TYPES = ['Service', 'OthCharge', 'Discount', 'Markup', 'Subtotal', 'Description', 'Payment', 'ShipItem', 'TaxItem', 'TaxGroup'];
        const SKIP_NAME = /shipping|freight|fee|setup|mold|tax|discount|duty|tariff/i;
        const lines = [];
        const n = po.getLineCount({ sublistId: 'item' });
        for (let i = 0; i < n && lines.length < 200; i++) {
          const get = (f) => po.getSublistValue({ sublistId: 'item', fieldId: f, line: i });
          const itemType = text(get('itemtype'));
          const itemName = text(po.getSublistText({ sublistId: 'item', fieldId: 'item', line: i }));
          if (SKIP_TYPES.includes(itemType) || SKIP_NAME.test(itemName)) continue;
          const key = text(get('lineuniquekey'));
          if (!/^[1-9][0-9]*$/.test(key)) continue;
          const qty = Number(get('quantity'));
          lines.push({
            lineKey: key,
            productName: text(get('custcol_om_product_name')),
            description: text(get('custcol_sales_order_description')) || text(get('description')),
            variant: text(get('custcol_om_variant')),
            sizes: text(get('custcol_om_sizes')),
            quantity: isFinite(qty) ? qty : null,
            units: text(po.getSublistText({ sublistId: 'item', fieldId: 'units', line: i }))
          });
        }

        const body = JSON.stringify({
          version: 1,
          account: runtime.accountId,
          po: {
            poId, poNumber,
            vendorId: String(po.getValue('entity')),
            vendor: text(po.getText('entity')),
            soNumber, customer, shipTo
          },
          lines
        });

        const script = runtime.getCurrentScript();
        const url = script.getParameter({ name: 'custscript_om_po_ironbot_url' });
        const secretId = script.getParameter({ name: 'custscript_om_po_secret_id' });
        const ts = String(Math.floor(Date.now() / 1000));
        const hmac = crypto.createHmac({
          algorithm: crypto.HashAlg.SHA256,
          key: crypto.createSecretKey({ secret: secretId, encoding: encode.Encoding.UTF_8 })
        });
        hmac.update({ input: ts + '.' + body, inputEncoding: encode.Encoding.UTF_8 });
        const signature = hmac.digest({ outputEncoding: encode.Encoding.HEX });

        const res = https.post({
          url, body,
          headers: { 'Content-Type': 'application/json', 'X-OM-Timestamp': ts, 'X-OM-Signature': signature }
        });
        log.audit('PO to Master PO List', { po: poNumber, code: res.code, body: (res.body || '').slice(0, 300) });
      } catch (e) {
        // Never block the PO save; the 5-minute Iron Bot sync is the backup.
        log.error('PO to Master PO List failed', e.message || String(e));
      }
    }

    return { afterSubmit };
  });
