import{z as n,l as s}from"./index-BanltRgW.js";/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const c=n("MapPin",[["path",{d:"M20 10c0 4.993-5.539 10.193-7.399 11.799a1 1 0 0 1-1.202 0C9.539 20.193 4 14.993 4 10a8 8 0 0 1 16 0",key:"1r0f0z"}],["circle",{cx:"12",cy:"10",r:"3",key:"ilqhr7"}]]);function x({venue:t,kickoff:a}){const e=a&&/T(\d{2}):(\d{2})(?::\d{2})?(Z|[+-]\d{2}:\d{2})$/.exec(a);if(!t&&!e)return null;const r=e?Number(e[1]):0,i=e&&(e[3]==="Z"?"UTC":`UTC${e[3]}`);return s.jsxs("div",{className:"flex flex-wrap items-center gap-x-2 gap-y-1 text-slate-400 text-[11px] font-mono",children:[t&&s.jsxs("span",{className:"inline-flex items-center gap-1",children:[s.jsx(c,{className:"w-3 h-3 shrink-0","aria-hidden":!0}),t]}),e&&s.jsxs("time",{dateTime:a,children:[r%12||12,":",e[2]," ",r<12?"AM":"PM"," (",i,")"]})]})}export{c as M,x as P};
