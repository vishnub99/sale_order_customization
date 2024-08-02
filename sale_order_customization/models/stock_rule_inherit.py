import logging
from collections import defaultdict
from odoo import SUPERUSER_ID, _, api, models
from odoo.tools import float_compare
from itertools import groupby


_logger = logging.getLogger(__name__)


class ProcurementException(Exception):
    """An exception raised by ProcurementGroup `run` containing all the faulty
    procurements.
    """

    def __init__(self, procurement_exceptions):
        """:param procurement_exceptions: a list of tuples containing the faulty
        procurement and their error messages
        :type procurement_exceptions: list
        """
        self.procurement_exceptions = procurement_exceptions


class StockRule(models.Model):
    _inherit = 'stock.rule'

    @api.model
    def _run_pull(self, procurements):
        moves_values_by_company = defaultdict(list)
        mtso_products_by_locations = defaultdict(list)

        # To handle the `mts_else_mto` procure method, we do a preliminary loop to
        # isolate the products we would need to read the forecasted quantity,
        # in order to to batch the read. We also make a sanitary check on the
        # `location_src_id` field.
        for procurement, rule in procurements:
            if not rule.location_src_id:
                msg = _('No source location defined on stock rule: %s!') % (rule.name,)
                raise ProcurementException([(procurement, msg)])

            if rule.procure_method == 'mts_else_mto':
                mtso_products_by_locations[rule.location_src_id].append(procurement.product_id.id)

        # Get the forecasted quantity for the `mts_else_mto` procurement.
        forecasted_qties_by_loc = {}
        for location, product_ids in mtso_products_by_locations.items():
            products = self.env['product.product'].browse(product_ids).with_context(location=location.id)
            forecasted_qties_by_loc[location] = {product.id: product.free_qty for product in products}

        # Prepare the move values, adapt the `procure_method` if needed.
        procurements = sorted(procurements, key=lambda proc: float_compare(proc[0].product_qty, 0.0,
                                                                           precision_rounding=proc[
                                                                               0].product_uom.rounding) > 0)
        for procurement, rule in procurements:
            procure_method = rule.procure_method
            if rule.procure_method == 'mts_else_mto':
                qty_needed = procurement.product_uom._compute_quantity(procurement.product_qty,
                                                                       procurement.product_id.uom_id)
                if float_compare(qty_needed, 0, precision_rounding=procurement.product_id.uom_id.rounding) <= 0:
                    procure_method = 'make_to_order'
                    for move in procurement.values.get('group_id', self.env['procurement.group']).stock_move_ids:
                        if move.rule_id == rule and float_compare(move.product_uom_qty, 0,
                                                                  precision_rounding=move.product_uom.rounding) > 0:
                            procure_method = move.procure_method
                            break
                    forecasted_qties_by_loc[rule.location_src_id][procurement.product_id.id] -= qty_needed
                elif float_compare(qty_needed, forecasted_qties_by_loc[rule.location_src_id][procurement.product_id.id],
                                   precision_rounding=procurement.product_id.uom_id.rounding) > 0:
                    procure_method = 'make_to_order'
                else:
                    forecasted_qties_by_loc[rule.location_src_id][procurement.product_id.id] -= qty_needed
                    procure_method = 'make_to_stock'

            move_values = rule._get_stock_move_values(*procurement)

            move_values['procure_method'] = procure_method
            moves_values_by_company[procurement.company_id.id].append(move_values)

        for company_id, moves_values in moves_values_by_company.items():
            product_name = []
            for values in moves_values:
                product_name.append(values['name'])
            product_name.sort()
            res = [list(i) for j, i in groupby(product_name, lambda a: a.partition(' ')[0])]

            for i_product in res:
                vals = {}
                move_ids = []
                for values in moves_values:
                    if values['name'] in i_product:
                        move_ids.append((0, 0, values))
                        vals.update({
                            'partner_id': values['partner_id'],
                            'picking_type_id': values['picking_type_id'],
                            'location_id': values['location_id'],
                            'location_dest_id': values['location_dest_id'],
                            'origin': values['origin'],
                            'group_id': values['group_id'],
                            'move_ids': move_ids
                        })

                vals.update({
                    'move_ids': move_ids
                })

                picking_id = (self.env['stock.picking'].with_user(SUPERUSER_ID).sudo().
                              with_company(company_id).create(vals))
                for line in picking_id.move_ids:
                    line._action_confirm()

        return True
