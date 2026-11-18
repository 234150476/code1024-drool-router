export const defaultQuarantine={enabled:false,cooldown_minutes:60,hide_failed_results:true};
export const gradableQuestion=q=>Boolean(q?.enabled)&&['number','choice','json'].includes(q.kind);
export const policyForQuestions=(policy,questions)=>({...defaultQuarantine,...policy,enabled:Boolean(policy?.enabled)&&questions.some(gradableQuestion)});
export const managedState=state=>!['manual','recovered'].includes(state);
export const quarantineState=state=>({disabling:'正在剔除',cooling:'冷却中',rechecking:'正在复测',restoring:'正在恢复',recovered:'已恢复',manual:'已转人工',blocked:'需要管理员处理'}[state]||state);
export const quarantineError=code=>['HTTP_403','QUARANTINE_HTTP_403'].includes(code)?'网关拒绝修改调度，请检查专用 Key 的 accounts.status 权限及授权范围。':code;
