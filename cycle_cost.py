"""Cycle-local, sanitized LLM accounting. No prompts or API bodies are retained."""
import asyncio
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation
import time

CURRENT_CYCLE = ContextVar('newsbot_cycle_cost', default=None)

def decimal_cost(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        cost = Decimal(str(value))
        return cost if cost.is_finite() and cost >= 0 else None
    except (InvalidOperation, ValueError):
        return None

def token_count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

class CycleReport:
    def __init__(self, db, manual, operation_type='collection'):
        self.db = db
        self.operation_type = operation_type
        self.owner = asyncio.current_task()
        self.report = {'workspace_id': db.workspace_id, 'operation_type': operation_type, 'started_at': int(time.time()), 'finished_at': None,
                       'manual': bool(manual), 'status': 'running', 'attempts': [],
                       'calls': 0, 'input_tokens': 0, 'output_tokens': 0,
                       'tokens_complete': True, 'cost_usd': None,
                       'known_cost_usd': '0', 'cost_complete': False,
                       'provenance': 'pending', 'result': None}
        self.id = (db.start_cycle if operation_type == 'collection' else db.start_operation)(self.report)
        self.report['id'] = self.id
        self.save()

    def attempt(self, provider, model, stage, input_rate, output_rate):
        a = {'provider': provider, 'model': model, 'stage': stage, 'status': 'pending',
             'input_tokens': None, 'output_tokens': None, 'cost_usd': None,
             'provenance': 'unknown', 'input_usd_per_million': input_rate or None,
             'output_usd_per_million': output_rate or None}
        self.report['attempts'].append(a)
        self.save()
        return a

    def response(self, attempt, response, provider_cost_supported=False, local_no_bill=False):
        usage = response.usage
        if usage is not None:
            attempt['input_tokens'] = token_count(usage.prompt_tokens)
            attempt['output_tokens'] = token_count(usage.completion_tokens)
        rates = [decimal_cost(attempt[k]) for k in ('input_usd_per_million', 'output_usd_per_million')]
        if all(r is not None and r > 0 for r in rates) and all(
                attempt[k] is not None for k in ('input_tokens', 'output_tokens')):
            attempt['cost_usd'] = str((rates[0] * attempt['input_tokens'] +
                                       rates[1] * attempt['output_tokens']) / Decimal(1000000))
            attempt['provenance'] = 'tariff_estimate'
        reported = decimal_cost(getattr(usage, 'cost', None)) if provider_cost_supported else None
        if local_no_bill:
            attempt['cost_usd'] = '0'
            attempt['provenance'] = 'local_no_bill'
        if reported is not None:
            attempt['cost_usd'] = str(reported)
            attempt['provenance'] = 'provider_reported'
        attempt['status'] = 'completed'
        self.save()

    def save(self):
        attempts = self.report['attempts']
        self.report['calls'] = len(attempts)
        for key in ('input_tokens', 'output_tokens'):
            self.report[key] = sum(a[key] or 0 for a in attempts)
        self.report['tokens_complete'] = all(a['input_tokens'] is not None and
                                             a['output_tokens'] is not None for a in attempts)
        complete = all(a['cost_usd'] is not None for a in attempts)
        known = sum((Decimal(a['cost_usd']) for a in attempts if a['cost_usd'] is not None), Decimal(0))
        self.report['known_cost_usd'] = str(known)
        self.report['cost_complete'] = complete and self.report['status'] != 'running'
        self.report['cost_usd'] = str(known) if self.report['cost_complete'] else None
        kinds = {a['provenance'] for a in attempts}
        self.report['provenance'] = ('pending' if self.report['status'] == 'running' else
                                     'no_calls' if not attempts else
                                     'unknown/incomplete' if not complete else
                                     next(iter(kinds)) if len(kinds) == 1 else 'mixed')
        (self.db.save_cycle if self.operation_type == 'collection' else self.db.save_operation)(self.id, self.report)

    def finish(self, status, result=None):
        self.report.update(status=status, finished_at=int(time.time()), result=result)
        self.save()


def active_cycle():
    cycle = CURRENT_CYCLE.get()
    # A spawned scheduler/module task inherits ContextVar, but is not this cycle.
    return cycle if cycle and cycle.owner is asyncio.current_task() and cycle.report['status'] == 'running' else None
