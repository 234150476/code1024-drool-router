import React, {useEffect, useState} from 'react';
import {Activity, BookOpen, Check, ChevronDown, Copy, ExternalLink, LockKeyhole, Moon, RefreshCw, Settings2, Sun, TriangleAlert} from 'lucide-react';
import {apiFetch, saveAccessToken} from './access.js';
import {endpoint} from './endpoints.js';
import {useI18n} from './i18n.js';

export function ThemeSwitch() {
  const {language} = useI18n();
  const [theme, setTheme] = useState(() => {
    const param = new URLSearchParams(location.search).get('theme');
    if (['light', 'dark'].includes(param)) return param;
    try { return localStorage.getItem('drool-theme') || 'light'; } catch { return 'light'; }
  });
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem('drool-theme', theme); } catch { /* Optional preference. */ }
  }, [theme]);
  const label = language === 'zh' ? '切换深浅色' : 'Toggle theme';
  return <button className="icon-button" title={label} aria-label={label} onClick={() => setTheme(t => t === 'dark' ? 'light' : 'dark')}>{theme === 'dark' ? <Sun size={17}/> : <Moon size={17}/>}</button>;
}

export function AccessForm({onAccess}) {
  const [token, setToken] = useState(''), [busy, setBusy] = useState(false), [error, setError] = useState('');
  return <main className="access-shell"><form className="access-panel" onSubmit={async event => {event.preventDefault(); setBusy(true); setError(''); saveAccessToken(token); const ok=await onAccess(); if(!ok)setError('连接未成功，请核对口令和服务状态。'); setBusy(false);}}>
    <LockKeyhole size={28}/><h1>连接质量巡检</h1><p>输入此独立服务的访问口令。口令由部署者配置，与 Sub2API 管理员密钥不同。</p>
    <label>服务访问口令<input autoFocus type="password" autoComplete="current-password" value={token} onChange={event => setToken(event.target.value)} required/></label>
    <button className="command" type="submit" disabled={busy}>{busy?'正在连接…':'打开巡检台'}</button>{error&&<p role="alert">{error}</p>}<small>仅保存在当前浏览器标签页的会话中。</small>
  </form></main>;
}

const REFERENCES = [
  {id: 'reasoning', title: '逻辑与数学', label: '可核对答案', text: '糖果题适合做轻量哨兵，单题通过率不能代表完整推理能力。使用多道等价题，记录首答、复核和失败原因。',
   prompt: '有红、蓝、绿三种颜色的袜子各 10 只。不开灯且无法分辨颜色，至少取出多少只才能保证有 2 双同色袜子（两双可同色）？请给出最坏情况证明，最后一行使用“最终答案：N”。', expected: '6。最多 5 只仍可能为 3、1、1；任取 6 只必有两对。',
   source: 'GSM8K', url: 'https://github.com/openai/grade-school-math'},
  {id: 'instructions', title: '指令与格式遵循', label: '结构校验', text: '把内容正确与格式正确分开统计；工具调用、JSON 字段、禁止多余文本都能作客观检查。',
   prompt: '仅返回一个 JSON 对象，不要 Markdown 或解释。包含且仅包含 keys: cities, count。cities 为 ["北京","上海","深圳"] 按此顺序；count 为城市数的整数。', expected: '{"cities":["北京","上海","深圳"],"count":3}。严格解析 JSON 并核对字段和值。',
   source: 'IFEval', url: 'https://github.com/google-research/google-research/tree/master/instruction_following_eval'},
  {id: 'context', title: '长上下文与信息提取', label: '条件对照', text: '在不同长度文档的开头、中间、结尾埋入新生成的标识。检查准确率、截断与召回位置；不能把 token 上限截断当成模型退化。',
   prompt: '准备 4k / 16k / 32k token 的文档，在三个位置插入随机、互不重复的订单编号及金额。要求仅输出这些订单编号与金额的 JSON。每轮更换编号并保留期望值。', expected: '按预先保存的编号与金额逐项精确核对；上面是构造实验的步骤，不是可直接评分的题目。',
   source: 'RULER', url: 'https://github.com/NVIDIA/RULER'},
  {id: 'code', title: '代码与单元测试', label: '执行验证', text: '用固定运行时、固定测试用例计算通过率。源码长度、注释数量和“看上去能跑”不能替代执行结果。',
   prompt: '实现 Python 函数 merge_intervals(intervals)，合并闭区间且按起点排序。空输入返回 []，相接端点也要合并，不得修改输入。只输出代码。', expected: '至少覆盖空数组、乱序、包含、端点相接、负数和输入不变性。请在隔离环境中运行，当前服务不会自动执行模型代码。',
   source: 'HumanEval', url: 'https://github.com/openai/human-eval'},
];

export function QualityGuide() {
  const [copied, setCopied] = useState('');
  return <section className="quality-guide">
    <div className="section-intro"><BookOpen size={20}/><div><h2>降智检测参考</h2><p>用可重复的证据观察变化。以下是补充测试建议，不计入当前巡检分数。</p></div></div>
    <div className="evidence-strip"><div><strong>答题失败</strong><span>有明确答案但回答错误，计入任务准确率。</span></div><div><strong>请求故障</strong><span>超时、429、鉴权与限额单列，排查链路。</span></div><div><strong>绘图完整性</strong><span>HTML/SVG 完整返回；画面质量仍需人工比较。</span></div></div>
    <div className="reference-grid">{REFERENCES.map(item => <article className="reference" key={item.id}><div className="reference-heading"><h3>{item.title}</h3><span>{item.label}</span></div><p>{item.text}</p><details><summary>查看测试样例 <ChevronDown size={14}/></summary><pre>{item.prompt}</pre><p className="answer-note">核验：{item.expected}</p><button className="text-button" onClick={async () => {try {await navigator.clipboard.writeText(item.prompt); setCopied(item.id);} catch {setCopied('failed');}}}>{copied === item.id ? <Check size={14}/> : <Copy size={14}/>} {copied === item.id ? '已复制' : '复制测试提示词'}</button>{copied === 'failed' && <p role="status">浏览器未允许复制，请手动选择文本。</p>}</details><a className="reference-source" href={item.url} target="_blank" rel="noreferrer noopener">参考 {item.source}<ExternalLink size={12}/></a></article>)}</div>
    <div className="method-note"><TriangleAlert size={18}/><div><strong>如何判断变化更可信</strong><p>固定模型、推理等级、提示词、输出预算和网络条件，对照同一账号的历史；每个条件保留多次样本与样本数。换模型或换题库后重新建基线。单次错误、风格变化和模型自报名称均不足以证明“降智”或模型身份。</p><p>短题重复命中也不能证明所有请求都未缓存；新鲜请求标识仅表示本工具创建了独立请求。综合调度分还包含价格、延迟和可用性，不应当作纯智力分。</p></div></div>
  </section>;
}

export function ServicePanel({service, onRefresh}) {
  const [busy, setBusy] = useState(''), [message, setMessage] = useState(''), [confirm, setConfirm] = useState(false);
  async function start(action) {
    setBusy(action); setMessage('');
    try {
      const response = await apiFetch(endpoint('/api/jobs'), {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({action})});
      if (!response.ok) throw Error(response.status === 409 ? '已有任务正在执行。' : '无法启动任务，请核对网关配置、访问口令与服务日志。');
      setMessage(action === 'sync' ? '已开始同步账号，不会调用模型。' : '已开始本轮检测，同一时间段已完成的任务不会重复执行。');
      setConfirm(false); onRefresh();
    } catch (error) { setMessage(error.message); } finally {setBusy('');}
  }
  if (!service) return null;
  const disabled = Boolean(busy || service.job.running || !service.configured);
  return <section className="service-panel"><div className="section-intro"><Settings2 size={20}/><div><h2>服务与检测配置</h2><p>独立部署，配置来自 config.json 和环境变量；修改后重启本服务。</p></div></div>
    <div className="service-facts"><span>网关地址 <b>{service.gateway_configured ? '已配置' : '待配置'}</b></span><span>管理员凭据 <b>{service.session_auth ? '当前管理员会话' : service.credential_configured ? '已配置' : '待配置'}</b></span><span>定时检测 <b>{service.schedule_enabled ? '已开启' : '已关闭'}</b></span><span>调度回写 <b>{service.routing_writes.priority || service.routing_writes.callable ? '已开启' : '已关闭'}</b></span></div>
    {!service.configured && <div className="setup-note"><strong>先连接你的 Sub2API</strong><p>在 config.json 填写 base_url、启用的模型和分组范围，并通过 SUB2API_ADMIN_KEY_FILE 提供管理员凭据。完成后重启本服务，再同步账号。</p><p>若账号导出受二次认证保护，可使用本地凭据文件 SUB2API_ACCOUNT_CREDENTIALS_FILE，保持 Sub2API 原有认证策略。</p></div>}
    <dl className="config-list"><div><dt>检测间隔</dt><dd>常规 {service.regular_minutes} 分钟 / 安静时段 {service.quiet_minutes} 分钟（{service.timezone}）</dd></div><div><dt>启用模型</dt><dd>{service.platforms.length ? service.platforms.map(p => `${p.id}: ${p.model || '未设置'}${p.group_ids.length ? ` / 分组 ${p.group_ids.join(', ')}` : ' / 所有分组'}`).join('；') : '未启用平台'}</dd></div><div><dt>允许嵌入</dt><dd>{service.frame_ancestors.join('、') || '仅允许同源；在 web.frame_ancestors 填入你的 Sub2API 域名'}</dd></div></dl>
    <div className="service-actions"><button className="command ghost" disabled={disabled} onClick={() => start('sync')}><RefreshCw size={15}/>同步账号</button><button className="command" disabled={disabled} onClick={() => setConfirm(true)}><Activity size={15}/>运行本轮检测</button>{service.job.running && <span role="status">{service.job.action === 'sync' ? '正在同步账号…' : '正在检测…'}</span>}</div>
    {confirm && <div className="run-confirm" role="group" aria-label="确认模型调用"><p>会对配置范围内的账号发起真实请求并消耗额度。通常每账号为两次逻辑题和一次绘图，答错会提前结束，失败重试可能增加请求数。{(service.routing_writes.priority || service.routing_writes.callable) && '当前配置还允许修改 Sub2API 调度状态或优先级。'}</p><button className="command" disabled={disabled} onClick={() => start('run')}>确认运行</button><button className="text-button" onClick={() => setConfirm(false)}>取消</button></div>}
    {message && <p className="job-message" role="status">{message}</p>}{service.job.ok === false && <p className="job-message" role="alert">最近一次任务未成功，请查看 private/worker.log；同步失败不会视为模型答错。</p>}
  </section>;
}
