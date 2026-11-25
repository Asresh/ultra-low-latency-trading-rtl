"""Reference an atomic, age-qualified FX sheet using exact rational arithmetic."""
from argparse import ArgumentParser
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from random import Random

from notional_model import rounded_divide


@dataclass(frozen=True)
class Quote:
    row: int
    enabled: int
    src: int
    dst: int
    bid: int
    ask: int
    observed: int


@dataclass(frozen=True)
class Request:
    tag: int
    src: int
    dst: int
    via: bool
    pivot: int
    age: int
    amount: int
    rounding: int


def encode(*fields):
    value = 0
    for field, width in fields:
        value = (value << width) | (int(field) & ((1 << width)-1))
    return value


def convert(rows, now, request, currencies, rows_width, fraction):
    req = request
    no_row = (1 << rows_width)-1
    def output(status, amount=0, first=None, second=None, age=0, inexact=False):
        return (status,amount,int(first is not None)+int(second is not None),
                no_row if first is None else first,no_row if second is None else second,age,inexact)

    pivot_ok = req.via and req.pivot < currencies and req.pivot not in (req.src, req.dst)
    if (max(req.src,req.dst) >= currencies or req.rounding == 3 or
            (req.via and not pivot_ok and req.src != req.dst)):
        return 1, output(1)
    if req.src == req.dst:
        return 1, output(0, req.amount)

    def query(src, dst):
        matching = sorted((q for q in rows.values() if q.enabled and (q.src,q.dst)==(src,dst)),
                          key=lambda q:q.row)
        usable = [x for x in matching if 0 <= now-x.observed <= req.age]
        return (usable[0] if usable else None),bool(matching)

    first, direct_exists = query(req.src,req.dst)
    second = None
    if not first:
        (via_a, a_exists),(via_b,b_exists) = (
            (query(req.src,req.pivot),query(req.pivot,req.dst)) if pivot_ok
            else ((None,False),(None,False)))
        if not via_a or not via_b:
            return 1, output(3 if direct_exists or (a_exists and b_exists) else 2)
        first,second = via_a,via_b

    scale = 1 << fraction
    price = Fraction(first.ask if req.amount >= 0 else first.bid,scale)
    if second:
        price *= Fraction(second.ask if req.amount >= 0 else second.bid,scale)
    exact = abs(req.amount)*price
    rounded = rounded_divide(exact.numerator,exact.denominator,req.rounding)
    signed = -rounded.amount if req.amount < 0 else rounded.amount
    age = now-min(first.observed,second.observed if second else first.observed)
    status = int(not -(1 << 64) <= signed < (1 << 64))*4
    return 3, output(status,signed if status == 0 else 0,first.row,
                     second.row if second else None,age,rounded.inexact)


def tape(path, currencies, rows_count, rate_width, fraction, seed, cycles):
    rng = Random(seed)
    tables = [{},{}]
    ccy_width, row_width = currencies.bit_length(), rows_count.bit_length()
    time_width, epoch_width = 16, 3
    epoch, bank, config_error = 0,0,0
    pending, source = None,None
    held_config = None
    statuses, hops, offered = Counter(), Counter(), 0
    scale = 1 << fraction

    with path.open("w") as out:
        for i in range(cycles):
            now = 100+i
            commit = False
            config = None
            offer_ready = (i<36 or rng.randrange(3)!=0)
            if i < 36:
                make = {
                    0: Quote(0,1,0,2,scale//2,scale,2),
                    1: Quote(1,1,0,1,scale,scale+scale//2,83),
                    2: Quote(2,1,1,2,scale//2,scale,85),
                    3: Quote(3,1,0,2,scale//2,scale//2,115),
                    4: Quote(4,1,2,0,2*scale,2*scale,100),
                    17: Quote(0,1,0,2,scale,2*scale,116),
                    18: Quote(1,1,0,2,2*scale,scale,117),
                    22: Quote(4,1,2,1,scale,scale,118),
                }
                config = make.get(i)
                commit = i in (5,22,31)
                direct = Request(i,0,2,False,0,32,3,2)
                stimulus = {
                    5: direct,
                    8: Request(i,0,2,True,1,40,-3,2),
                    14: Request(i,2,0,False,0,50,(1<<64)-1,0),
                    20: direct,
                    27: Request(i,1,1,True,1,0,-(1<<64),1),
                    30: Request(i,currencies,0,False,0,1,22,0),
                    33: Request(i,2,1,False,0,700,10,0),
                }
                if source is None:
                    source = stimulus.get(i)
            else:
                commit = rng.randrange(12)==0
                if rng.randrange(3)!=0:
                    row = rng.randrange(rows_count if rng.randrange(15) else (1<<row_width))
                    a,b = rng.sample(range(currencies), 2)
                    price = rng.randrange(1,1<<rate_width)
                    ask = rng.randrange(price,1<<rate_width)
                    if rng.randrange(17)==0:
                        ask = price-1
                    config = Quote(row,int(rng.randrange(13)!=0),a,b,price,ask,max(0, now+rng.randint(-90,12)))
                if source is None and rng.randrange(4)!=0:
                    a,b,pivot = (rng.randrange(currencies) for _ in range(3))
                    raw = rng.choice((3,-3,43,-73,rng.randrange(-(1<<64),1<<64)))
                    source = Request(i,a,b,bool(rng.randrange(3)==0),pivot,
                                     rng.randint(3,100),raw,rng.choice((0,1,2,2,2,3)))

            if held_config is not None:
                config = held_config
            cfg_ready = not commit
            held_config = config if commit else None
            input_ready = pending is None
            result = 0
            if pending and pending[0] <= i:
                _,tag,resp_epoch,fields = pending
                status,amount,count,row_a,row_b,age,inexact = fields
                result=encode((1,1),(tag,16),(status,3),(amount,65),(count,2),
                              (row_a,row_width),(row_b,row_width),(age,time_width),(resp_epoch,epoch_width),(inexact,1))
                if offer_ready:
                    offered += 1
                    statuses[status] += 1
                    hops[count] += 1
                    pending=None

            q = config or Quote(0,0,0,0,0,0,0)
            cfg=encode((config is not None,1),(q.row,row_width),(q.enabled,1),
                       (q.src,ccy_width),(q.dst,ccy_width),(q.bid,rate_width),
                       (q.ask,rate_width),(q.observed,time_width))
            s = source or Request(0,0,0,False,0,0,0,0)
            cmd=encode((source is not None,1),(s.tag,16),(s.src,ccy_width),(s.dst,ccy_width),
                       (s.via,1),(s.pivot,ccy_width),(s.age,time_width),(s.amount,65),(s.rounding,2))
            fields=(now,offer_ready,commit,cfg,cmd,cfg_ready,config_error,epoch,input_ready,result)
            out.write(" ".join(f"{int(i):x}" for i in fields)+"\n")
            if source and input_ready:
                delay, response = convert(tables[bank],now,source,currencies,row_width,fraction)
                pending=(i+delay,source.tag,epoch,response)
                source=None

            config_error=0
            if commit:
                bank ^= 1
                epoch=(epoch+1) % (1<<epoch_width)
            elif config:
                if config.row >= rows_count or (config.enabled and
                        (max(config.src,config.dst)>=currencies or config.src==config.dst or
                         not config.bid or config.bid>config.ask)):
                    config_error=1
                else:
                    tables[bank^1][config.row]=config
    print(f"FX oracle {cycles} cycles, {offered} results, outcomes={dict(statuses)}, hops={dict(hops)}")


def main():
    parser=ArgumentParser(description=__doc__)
    parser.add_argument("path",type=Path)
    parser.add_argument("--ccys",type=int,required=True)
    parser.add_argument("--rows",type=int,required=True)
    parser.add_argument("--rate",type=int,required=True)
    parser.add_argument("--fraction",type=int,required=True)
    parser.add_argument("--seed",type=int,required=True)
    parser.add_argument("--cycles",type=int,default=1900)
    a=parser.parse_args()
    tape(a.path,a.ccys,a.rows,a.rate,a.fraction,a.seed,a.cycles)


if __name__=="__main__":
    main()
