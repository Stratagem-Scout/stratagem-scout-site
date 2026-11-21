import{z as c,l as t}from"./index-CP66PWMS.js";/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const l=c("MapPin",[["path",{d:"M20 10c0 4.993-5.539 10.193-7.399 11.799a1 1 0 0 1-1.202 0C9.539 20.193 4 14.993 4 10a8 8 0 0 1 16 0",key:"1r0f0z"}],["circle",{cx:"12",cy:"10",r:"3",key:"ilqhr7"}]]);function d({venue:n,kickoff:s}){const e=s&&/T(\d{2}):(\d{2})(?::\d{2})?(Z|[+-]\d{2}:\d{2})$/.exec(s),r=s&&/^(TBD|TBA)$/i.test(s.trim());if(!n&&!e&&!r)return null;const a=e?Number(e[1]):0,i=e&&(e[3]==="Z"?"UTC":`UTC${e[3]}`);return t.jsxs("div",{className:"flex flex-wrap items-center gap-x-2 gap-y-1 text-slate-400 text-[11px] font-mono",children:[n&&t.jsxs("span",{className:"inline-flex items-center gap-1",children:[t.jsx(l,{className:"w-3 h-3 shrink-0","aria-hidden":!0}),n]}),r&&t.jsxs("span",{children:["Kickoff ",s]}),e&&t.jsxs("time",{dateTime:s,children:[a%12||12,":",e[2]," ",a<12?"AM":"PM"," (",i,")"]})]})}export{l as M,d as P};
