import React from 'react'

// 原始日志浏览（文本先行，等宽滚动）
export default function RawLog({ text }) {
  return <pre className="rawlog">{text}</pre>
}
