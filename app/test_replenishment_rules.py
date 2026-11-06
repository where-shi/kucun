"""Tests use fabricated values and never access the user's data."""
import copy
from datetime import date, timedelta
import unittest

from backend import high_return_styles, planning_recommendation, winter_momentum_styles


def sample():
    return {'sku':'DEMO-STYLE-Blue-9', 'style':'DEMO-STYLE', 'size':'9',
            'n7':2, 'p7':2, 'n14':4, 'n28':14,
            'inventoryKnown':True, 'inventoryCapturedAtISO':'2026-11-15T08:00:00+00:00',
            'inventory':{'afn_fulfillable_quantity':0, 'afn_inbound_shipped_quantity':0,
                         'afn_inbound_receiving_quantity':0, 'afn_inbound_working_quantity':0,
                         'reserved_fc_transfers':0, 'reserved_fc_processing':0}}


class PortableRecommendationTests(unittest.TestCase):
    def test_unmet_demand_advances_and_new_supply_resets_stage(self):
        row=sample();first=planning_recommendation(row)
        history=[{'date':date(2026,11,15)-timedelta(days=i),'stock':0,'pending':0,'n28':14} for i in range(1,8)]
        later=planning_recommendation(row,history=history)
        self.assertGreater(later['quantity'], first['quantity'])
        row['inventory']['afn_fulfillable_quantity']=2
        self.assertEqual(planning_recommendation(row,history=history)['stageDays'],0)

    def test_cost_return_and_packing_do_not_create_demand(self):
        row=sample();row.update(n7=0,p7=0,n14=0,n28=1)
        normal=planning_recommendation(row)['quantity']
        affordable=planning_recommendation(row,unit_cost_cents=3500)['quantity']
        self.assertGreaterEqual(affordable,normal)
        self.assertEqual(affordable,3)
        row.update(n28=14,orderMultiple=5)
        risk={'eligible':True,'sold':30,'returned':9,'rate':0.3,'salesEnd':'2026-11-14'}
        base=planning_recommendation(row);adjusted=planning_recommendation(row,return_risk=risk)
        self.assertLessEqual(adjusted['quantity'],base['quantity'])
        self.assertEqual(adjusted['quantity']%5,0)
        row.update(n7=0,p7=0,n14=0,n28=0)
        self.assertIsNone(planning_recommendation(row,unit_cost_cents=1000)['quantity'])

    def test_winter_and_size_rules_require_explicit_metadata(self):
        rows=[]
        for i in range(6):
            item=copy.deepcopy(sample());item['sku']=f'DEMO-STYLE-Blue-{i}';item['winterSeasonal']=True
            rows.append(item)
        self.assertIn('DEMO-STYLE',winter_momentum_styles(rows,'2026-11-14',date(2026,11,14),30,35))
        for item in rows:item.pop('winterSeasonal')
        self.assertEqual({},winter_momentum_styles(rows,'2026-11-14',date(2026,11,14),30,35))
        row=sample();row.update(size='6',n7=0,p7=0,n14=0,n28=4,sizeSystem='US_MEN')
        self.assertTrue(planning_recommendation(row)['sizeAdjustment'])
        row.pop('sizeSystem');self.assertFalse(planning_recommendation(row)['sizeAdjustment'])

    def test_return_risk_uses_fba_and_rejects_stale_report(self):
        rows=[sample()];report={'days':90,'channel':'FBA','basis':'period_fba','salesEnd':'2026-11-14',
          'rows':[{'sku':rows[0]['sku'],'sold':40,'returned':10}]}
        self.assertTrue(high_return_styles(report,rows,'2026-11-14',date(2026,11,14))['DEMO-STYLE']['eligible'])
        self.assertEqual({},high_return_styles(report,rows,'2026-11-14',date(2026,11,23)))

if __name__=='__main__': unittest.main()
