"""Passive reconciliation: no clicking, menu probing, model calls or replay."""
import re
from .backend_v2.repository import digest
from .form_observation import extraction_block_reason


def norm(value):
    return re.sub(r'\s+', '', str(value))


def reconcile_snapshot(snapshot, pending):
    outcomes=[]
    keys=('question_text','field_type','control_kind','section','entity_scope',
          'field_signature','container_key','control_group_key','record_evidence','option_label')
    for receipt in pending:
        if receipt['application_url']!=snapshot.url:continue
        if receipt['command_type'] not in {'set_text','select_option','set_date','check_option'}:continue
        target=receipt['target']
        if not target.get('question_text') or not target.get('field_signature'):continue
        matches=[f for f in snapshot.fields if all(getattr(f,k)==target.get(k,'') for k in keys)]
        if len(matches)!=1 or extraction_block_reason(matches[0]):continue
        field=matches[0];desired=receipt['desired']['value'];actual=field.current_value
        if isinstance(desired,bool):
            # A text caption alone does not prove a choice's checked state.
            if norm(actual).lower() not in {'true','false'}:continue
            equal=(norm(actual).lower()=='true')==desired
        elif field.multiple:
            split=lambda x:sorted(norm(i) for i in re.split('[,，\n]',str(x)) if norm(i))
            equal=split(actual)==split(desired)
        else:
            equal=norm(actual)==norm(desired)
        outcomes.append((receipt['id'],'verified' if equal else 'failed',
            {'readback_verified':equal,'passive_reconciliation':True,'actual_hash':digest(actual)}))
    return outcomes
