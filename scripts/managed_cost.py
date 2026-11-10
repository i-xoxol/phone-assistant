"""Reproducible planning estimate, not an account-specific cloud quote."""
import argparse
import json
from pathlib import Path


def estimate(active_hours=30, ancillary=10, free_grant=False, hours=730):
    if not 0 <= active_hours <= hours or ancillary < 0:
        raise ValueError('Use nonnegative costs and active hours within the month')
    rates=json.loads((Path(__file__).resolve().parents[1]/'deploy/managed/rates.json').read_text())
    a,w=rates['azure'],rates['aws']
    # Both designs: 0.5 vCPU/1 GiB application, managed DB with 2 GiB RAM.
    azure_compute=(active_hours*a['vcpu_active_second']+(hours-active_hours)*a['vcpu_idle_second'])*3600*.5
    azure_compute+=hours*3600*a['gib_second']
    # Grant is subscription-wide. Conservatively value CPU grant at idle price.
    if free_grant:azure_compute=max(0,azure_compute-180000*a['vcpu_idle_second']-360000*a['gib_second'])
    azure={'container':azure_compute,'postgres':hours*a['postgres_b1ms_hour']+32*a['postgres_gb_month'],
        'managed_network_lb_and_two_public_ips':hours*(a['managed_lb_hour']+2*a['public_ipv4_hour']),
        'files_logs_network_allowance':ancillary}
    aws={'fargate':hours*(.5*w['fargate_vcpu_hour']+w['fargate_gb_hour']),
        'rds':hours*w['rds_t4g_small_hour']+20*w['rds_storage_gb_month'],
        'alb_with_0_1_average_lcu':hours*(w['alb_hour']+.1*w['lcu_hour']),
        'public_ipv4_three_addresses':hours*3*w['public_ipv4_hour'],
        'efs_logs_secrets_allowance':ancillary}
    return {'checked':rates['checked'],'currency':'USD','month_hours':hours,
        'azure':{**{k:round(v,2) for k,v in azure.items()},'total':round(sum(azure.values()),2)},
        'aws_comparison_only':{**{k:round(v,2) for k,v in aws.items()},'total':round(sum(aws.values()),2)},
        'excluded':'Twilio, AI APIs, significant egress, taxes, HA, NAT gateway, subscription fees; no signup credits'}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--active-hours',type=float,default=30)
    p.add_argument('--ancillary',type=float,default=10,help='Estimated file/log/network costs, not a provider unit price')
    p.add_argument('--free-grant',action='store_true')
    args=p.parse_args()
    print(json.dumps(estimate(args.active_hours,args.ancillary,args.free_grant),indent=2))
