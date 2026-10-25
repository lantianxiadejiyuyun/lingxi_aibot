import { choice, field, type Field } from './ui'
export interface ReplyStyle {enabled:boolean;max_messages:number;interval_secs:number}
export interface Followup {enabled:boolean;delay_minutes:number;max_followups:number;daily_limit:number}
export interface Proactive {enabled:boolean;active_start:string;active_end:string;quiet_start:string;quiet_end:string;idle_minutes:number;min_interval_minutes:number;daily_limit:number;topics:string[]}
export interface Rhythm {reply_style?:ReplyStyle;followup?:Followup;proactive?:Proactive}
export function rhythmFields(c:Rhythm,group=false):Field[]{
 const fields:Field[]=[
  {...choice('split','一轮回复分成几条发',[['false','关闭 · 一条发完'],['true','开启 · 按完整意思分条']],!!c.reply_style?.enabled),hint:'模型用空行分段，一段一条；短回复仍可只发一条，不按句号硬拆。超出上限的尾段会合并，代码块保持完整。'},
  {...field('max_messages','一轮最多几条',c.reply_style?.max_messages??3,'number',true),min:2,max:4},
  {...field('interval_mode','段间节奏','随机 1–3 秒'),readonly:true,hint:'每段发送前随机稍等一下。QQ 连续输入会等安静 5 秒后合并处理，最长等 20 秒。'},
  {...choice('followup','让模型决定是否继续聊',[['false','关闭'],['true','开启 · 是否补充和次数由模型决定']],!!c.followup?.enabled),hint:'模型可以不发、发一次或继续补充；没有新内容就结束。收到新消息就取消，也会遵守活动和免打扰时段。'},
  {...field('delay_minutes','至少等多久再想一想（分钟）',c.followup?.delay_minutes??10,'number',true),min:1,max:60,hint:'到点只允许模型判断，不代表一定发消息。'},
  {...field('max_followups','一轮补充上限',c.followup?.max_followups??3,'number',true),min:1,max:5,hint:'防止刷屏的上限，模型不用发满。'},
  {...field('followup_daily','24小时补充上限',c.followup?.daily_limit??6,'number',true),min:1,max:12},
 ];
 if(group){const p=c.proactive;fields.push(
  {...choice('proactive','主动开启群话题',[['false','关闭'],['true','开启']],!!p?.enabled),hint:'每15分钟检查一次；只参考本群近24小时的公开聊天，没有合适话题就安静。没人接话时不连续开启新话题。'},
  field('active_start','活动开始',p?.active_start??'09:00','time',true),field('active_end','活动结束',p?.active_end??'22:00','time',true),
  field('quiet_start','免打扰开始',p?.quiet_start??'23:00','time',true),{...field('quiet_end','免打扰结束',p?.quiet_end??'08:00','time',true),hint:'使用账号时区。起止相同时，活动时段表示全天，免打扰表示关闭。延迟补充也遵守这些时段。'},
  {...field('idle_minutes','群里安静多久后可开场（分钟）',p?.idle_minutes??30,'number',true),min:5,max:1440},
  {...field('min_interval_minutes','两次主动开场至少间隔（分钟）',p?.min_interval_minutes??180,'number',true),min:30,max:1440},
  {...field('daily_limit','每天最多主动开场几次',p?.daily_limit??2,'number',true),min:1,max:6},
  {...field('topics','适合这个群的话题',(p?.topics??[]).join('\n'),'textarea'),hint:'一行一个，最多8个、每个40字。留空时参考当前群的近期话题。'}
 );} return fields.map(f=>({...f,advanced:!['split','followup','proactive'].includes(f.key)}));
}
export function rhythmValue(v:Record<string,string>,group=false):Rhythm{
 const value:Rhythm={reply_style:{enabled:v.split==='true',max_messages:Number(v.max_messages??3),interval_secs:Number(v.interval_secs??2)},followup:{enabled:v.followup==='true',delay_minutes:Number(v.delay_minutes??10),max_followups:Number(v.max_followups??3),daily_limit:Number(v.followup_daily??6)}};
 if(group)value.proactive={enabled:v.proactive==='true',active_start:v.active_start||'09:00',active_end:v.active_end||'22:00',quiet_start:v.quiet_start||'23:00',quiet_end:v.quiet_end||'08:00',idle_minutes:Number(v.idle_minutes??30),min_interval_minutes:Number(v.min_interval_minutes??180),daily_limit:Number(v.daily_limit??2),topics:(v.topics||'').split(/[,，\r\n]+/).map(s=>s.trim()).filter(Boolean)};
 return value;
}
