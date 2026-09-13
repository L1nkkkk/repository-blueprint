/* View projections and geometry. Never mutate the evidence graph. */
(function(root) {
  'use strict';
  const dataKinds = new Set(['data', 'read', 'write']);
  const index = rows => new Map(rows.map(row => [row.id, row]));
  const unique = rows => [...new Map(rows.map(row => [row.id, row])).values()];

  function contextOwner(graph, id) {
    const context = graph.contexts.find(c => c.id === id);
    if (context?.callee_id) return context.callee_id;
    const caller = graph.contexts.find(c => c.parent_id === id)?.caller_id;
    if (caller) return caller;
    const owners = new Set(graph.ports.filter(p => p.context_id === id).map(p => p.entity_id));
    return graph.entities.find(e => owners.has(e.id) && ['function', 'method'].includes(e.kind))?.id;
  }

  function scopeChoices(graph) {
    return graph.contexts.filter(c => !c.parent_id && contextOwner(graph, c.id)).map(c => ({
      id: c.id, entity: graph.entities.find(e => e.id === contextOwner(graph, c.id))
    }));
  }

  function scopedData(graph, scopeId) {
    const entities = index(graph.entities), ports = index(graph.ports), contexts = index(graph.contexts);
    const ownerId = contextOwner(graph, scopeId), owner = entities.get(ownerId), scope = contexts.get(scopeId);
    if (!owner || !scope) return {nodes: [], edges: [], folded: 0};
    const nodes = new Map(), bodyKey = `body:${scopeId}`;
    function add(key, entity, context, role, title, summary) {
      if (!nodes.has(key)) nodes.set(key, {key, entity, context, role, title, summary, ports: []});
      return nodes.get(key);
    }
    function endpoint(p, other, outgoing, record) {
      if (p.context_id === scope.parent_id) {
        const role = outgoing ? 'input' : 'output';
        const n = add(`boundary:${scopeId}:${role}`, owner, scopeId, role,
          outgoing ? '传入参数' : '返回与写回',
          outgoing ? `从 ${entities.get(scope.caller_id)?.name || '调用方'} 进入本次调用的值。` : '本次调用交还给调用方的数据；端口标明返回或写回。');
        return {node: n.key, port: {...other, source_port_id: other.id, id: `${n.key}:${other.id}`, direction: outgoing ? 'out' : 'in',
          annotation: outgoing ? '' : record.kind === 'write' ? '写回' : '返回'}};
      }
      let n;
      if (p.context_id === scopeId && p.entity_id === ownerId) {
        n = add(bodyKey, owner, scopeId, 'body', `${owner.name} · 函数体`);
      } else {
        const child = contexts.get(p.context_id)?.parent_id === scopeId;
        n = add(`${p.entity_id}@${p.context_id}`, entities.get(p.entity_id), p.context_id, child ? 'call' : 'entity');
        if (child) n.expandContext = p.context_id;
      }
      const c = contexts.get(p.context_id), otherC = contexts.get(other.context_id);
      const returning = outgoing && c?.parent_id === otherC?.id;
      return {node: n.key, port: {...p, annotation: record.kind === 'write' && outgoing ? '写回' : returning ? '返回' : ''}};
    }
    let edges = [], total = 0;
    for (const r of graph.relations) {
      if (!dataKinds.has(r.kind)) continue;
      total++;
      const a = ports.get(r.from_id), b = ports.get(r.to_id);
      if (a.context_id !== scopeId && b.context_id !== scopeId) continue;
      const eligible = p => p.context_id === scopeId || p.context_id === scope.parent_id || contexts.get(p.context_id)?.parent_id === scopeId;
      if (!eligible(a) || !eligible(b)) continue;
      const from = endpoint(a, b, true, r), to = endpoint(b, a, false, r);
      edges.push({id: r.id, record: r, records: [r], from: from.node, to: to.node, fromPort: from.port, toPort: to.port, data: true});
    }
    const represented = new Set(edges.map(e => e.record.id));

    // Only contract an unambiguous forwarding of the SAME identity in THIS
    // context. A derived identity or multiple incoming sources is not a relay.
    const remove = new Set(), replacements = [];
    for (const flow of graph.flows) {
      const incoming = edges.filter(e => e.to === bodyKey && e.record.flow_id === flow.id && e.from !== bodyKey);
      const outgoing = edges.filter(e => e.from === bodyKey && e.record.flow_id === flow.id && e.to !== bodyKey);
      const producer = ports.get(flow.producer_port_id);
      if (incoming.length !== 1 || !outgoing.length || producer?.entity_id === ownerId && producer?.context_id === scopeId) continue;
      const before = incoming[0];
      // Do not hide a real feedback path by turning it into a self edge.
      if (outgoing.some(e => e.to === before.from)) continue;
      remove.add(before.id);
      for (const after of outgoing) {
        remove.add(after.id);
        const records = unique([...before.records, ...after.records]);
        replacements.push({...after, id: `relay:${before.id}:${after.id}`, from: before.from, fromPort: before.fromPort, records, viaOwner: owner.name});
      }
    }
    edges = edges.filter(e => !remove.has(e.id)).concat(replacements);

    // A function producing derived values keeps its body and actual input ports.
    // Otherwise separate local supplies from receipts so caller bookkeeping
    // cannot look like an execution loop. No dependency is invented here.
    const transforms = graph.flows.some(f => f.derived_from.length && ports.get(f.producer_port_id)?.entity_id === ownerId && ports.get(f.producer_port_id)?.context_id === scopeId);
    if (!transforms) {
      for (const edge of edges) {
        if (edge.from === bodyKey) {
          const n = add(`supply:${scopeId}:${edge.to}`, owner, scopeId, 'source', `${owner.name} · 提供数据`, '本层提供给这个节点的值。数据来源可点击连线核对。');
          edge.from = n.key;
        }
        if (edge.to === bodyKey) {
          const n = add(`receipt:${scopeId}`, owner, scopeId, 'output', `${owner.name} · 接收结果`, '本层接收的结果。尚未记录后续去向的值保留在这里。');
          edge.to = n.key;
        }
      }
    }
    // A call without recorded data still remains reachable for expansion.
    for (const c of graph.contexts.filter(c => c.parent_id === scopeId)) {
      if (!graph.relations.some(r => r.kind === 'call' && r.context_id === c.id)) continue;
      const e = entities.get(c.callee_id), n = add(`${e.id}@${c.id}`, e, c.id, 'call');
      n.expandContext = c.id;
    }
    const used = new Set(edges.flatMap(e => [e.from, e.to]));
    for (const n of nodes.values()) if (n.expandContext) used.add(n.key);
    const shown = [...nodes.values()].filter(n => used.has(n.key));
    for (const edge of edges) {
      // A graph port can appear at two boundary displays; view IDs are local.
      for (const [side, direction] of [['from', 'out'], ['to', 'in']]) {
        const field = side + 'Port', p = edge[field];
        const port = {...p, source_port_id: p.source_port_id || p.id, id: `${edge[side]}|${p.id}|${direction}`, direction, flow_id: edge.record.flow_id};
        edge[field] = port;
        const n = nodes.get(edge[side]);
        if (!n.ports.some(p => p.id === port.id)) n.ports.push(port);
      }
      edge.annotation = edge.records.some(r => r.kind === 'write') ? '写回' :
        edge.records.some(r => {const a = ports.get(r.from_id), b = ports.get(r.to_id);return contexts.get(a.context_id)?.parent_id === b.context_id;}) ? '返回' : '';
    }
    const portOrder = new Map(graph.ports.map((p, i) => [p.id, i]));
    shown.forEach(n => n.ports.sort((a,b) => (portOrder.get(a.source_port_id)??0) - (portOrder.get(b.source_port_id)??0)));
    for(const n of shown.filter(n=>n.key===`boundary:${scopeId}:output`)) {
      const kinds=new Set(n.ports.map(p=>p.annotation));
      if(kinds.size===1)n.title=kinds.has('写回')?'写回调用方':'返回调用方';
    }
    return {nodes: shown, edges, folded: total - represented.size};
  }

  function arrange(nodes, edges, dimensions, existing = {}, reset = false) {
    if (!nodes.length) return {};
    // Condense strongly connected components before assigning dependency ranks.
    const adjacency = new Map(nodes.map(n => [n.key, []]));
    edges.forEach(e => {if (adjacency.has(e.from) && adjacency.has(e.to)) adjacency.get(e.from).push(e.to);});
    let sequence = 0; const ids = new Map(), low = new Map(), stack = [], active = new Set(), components = [], component = new Map();
    function visit(k) {
      ids.set(k, sequence); low.set(k, sequence++); stack.push(k); active.add(k);
      for (const t of adjacency.get(k)) {
        if (!ids.has(t)) {visit(t); low.set(k, Math.min(low.get(k), low.get(t)));}
        else if (active.has(t)) low.set(k, Math.min(low.get(k), ids.get(t)));
      }
      if (low.get(k) === ids.get(k)) {
        const group = []; let t;
        do {t = stack.pop(); active.delete(t); group.push(t); component.set(t, components.length);} while (t !== k);
        components.push(group);
      }
    }
    nodes.forEach(n => {if (!ids.has(n.key)) visit(n.key);});
    const order = new Map(nodes.map((n,i)=>[n.key,i])), nodeHints = new Map(nodes.map(n=>[n.key,n.columnHint]));
    const offsets = new Map(), widths = components.map(group=>{
      group.sort((a,b)=>order.get(a)-order.get(b));
      const hints=group.map(k=>nodeHints.get(k)),useHints=group.length>1&&hints.every(Number.isFinite)&&Math.max(...hints)-Math.min(...hints)<6;
      const width=useHints?Math.max(...hints)-Math.min(...hints)+1:Math.min(3,Math.ceil(Math.sqrt(group.length)));
      group.forEach((k,i)=>offsets.set(k,useHints?nodeHints.get(k)-Math.min(...hints):i%width));return width;
    });
    const parents = components.map(() => new Set()), ranks = new Map();
    edges.forEach(e => {const a = component.get(e.from), b = component.get(e.to);if (a !== undefined && b !== undefined && a !== b) parents[b].add(a);});
    function rank(c) {if (!ranks.has(c)) ranks.set(c, parents[c].size ? Math.max(...[...parents[c]].map(p => rank(p) + widths[p])) : 0);return ranks.get(c);}
    const columns = new Map(nodes.map(n => [n.key, rank(component.get(n.key))+offsets.get(n.key)]));
    for (const n of nodes.filter(n => ['source', 'input'].includes(n.role))) {
      const targets = adjacency.get(n.key).filter(k => component.get(k) !== component.get(n.key));
      if (targets.length) columns.set(n.key, Math.max(0, Math.min(...targets.map(k => columns.get(k))) - 1));
    }
    // Keep disconnected workflows in separate bands. Crossings inside a band
    // are reduced with alternating barycenter sweeps over dependency columns.
    const neighbors = new Map(nodes.map(n => [n.key, new Set()]));
    for (const e of edges) if (neighbors.has(e.from) && neighbors.has(e.to) && e.from !== e.to) {
      neighbors.get(e.from).add(e.to); neighbors.get(e.to).add(e.from);
    }
    const networks = [], seen = new Set();
    for (const n of nodes) if (!seen.has(n.key)) {
      const group = [n.key]; seen.add(n.key);
      for (let i = 0; i < group.length; i++) for (const k of neighbors.get(group[i])) if (!seen.has(k)) {seen.add(k); group.push(k);}
      networks.push(nodes.filter(n => group.includes(n.key)));
    }
    const positions = {}, placed = [];
    const size = k => dimensions[k] || {width: 242, height: 230};
    const collision = (a, b) => a.x < b.x + b.width + 32 && a.x + a.width + 32 > b.x && a.y < b.y + b.height + 42 && a.y + a.height + 42 > b.y;
    for (const n of nodes) if (existing[n.key] && (!reset || existing[n.key].locked)) {
      positions[n.key] = {...existing[n.key]}; placed.push({...size(n.key), ...positions[n.key]});
    }
    const maxWidth = Math.max(242, ...nodes.map(n => size(n.key).width)), step = maxWidth + 140;
    let bandTop = 0;
    for (const network of networks) {
      const levels = [...new Set(network.map(n => columns.get(n.key)))].sort((a,b) => a-b);
      const rows = new Map(levels.map(c => [c, network.filter(n => columns.get(n.key) === c)]));
      const slots = new Map(), updateSlots = c => rows.get(c).forEach((n,i) => slots.set(n.key, (i+.5)/rows.get(c).length));
      levels.forEach(updateSlots);
      for (let pass = 0; pass < 4; pass++) for (const direction of [1,-1]) for (const c of direction === 1 ? levels : levels.slice().reverse()) {
        const score = new Map(rows.get(c).map(n => {
          const adjacent = [...neighbors.get(n.key)].filter(k => direction*(c-columns.get(k)) > 0);
          return [n.key, adjacent.length ? adjacent.reduce((sum,k) => sum+slots.get(k), 0)/adjacent.length : slots.get(n.key)];
        }));
        rows.get(c).sort((a,b) => score.get(a.key)-score.get(b.key)); updateSlots(c);
      }
      for (const column of levels) {
        let cursor = bandTop;
        for (const n of rows.get(column)) {
          if (positions[n.key]) continue;
          const previous = [...neighbors.get(n.key)].filter(k => columns.get(k) < column && positions[k]);
          const center = previous.length ? previous.reduce((sum,k) => sum+positions[k].y+size(k).height/2,0)/previous.length-size(n.key).height/2 : bandTop;
          const box = {x: column*step, y: Math.max(cursor, center), ...size(n.key)};
          let hit;
          while ((hit = placed.find(p => collision(box,p)))) box.y = hit.y+hit.height+80;
          positions[n.key] = {x:box.x, y:box.y, locked:false}; placed.push(box); cursor = box.y+box.height+80;
        }
      }
      bandTop = Math.max(bandTop, ...network.map(n => positions[n.key].y+size(n.key).height))+160;
    }
    return positions;
  }

  const inside = (p, r) => p.x > r.x + .01 && p.x < r.x + r.width - .01 && p.y > r.y + .01 && p.y < r.y + r.height - .01;
  function intersects(a, b, r) {
    if (a.y === b.y) return a.y > r.y + .01 && a.y < r.y + r.height - .01 && Math.max(a.x, b.x) > r.x + .01 && Math.min(a.x, b.x) < r.x + r.width - .01;
    if (a.x === b.x) return a.x > r.x + .01 && a.x < r.x + r.width - .01 && Math.max(a.y, b.y) > r.y + .01 && Math.min(a.y, b.y) < r.y + r.height - .01;
    return false;
  }
  class Heap {
    constructor() {this.items = [];}
    push(item) {let i = this.items.length; this.items.push(item);while (i) {const p = (i - 1) >> 1;if (this.items[p].score <= item.score) break;this.items[i] = this.items[p];i = p;}this.items[i] = item;}
    pop() {const top = this.items[0], last = this.items.pop();if (this.items.length) {let i = 0;while (i * 2 + 1 < this.items.length) {let c = i * 2 + 1;if (c + 1 < this.items.length && this.items[c + 1].score < this.items[c].score) c++;if (this.items[c].score >= last.score) break;this.items[i] = this.items[c];i = c;}this.items[i] = last;}return top;}
  }
  function simplify(points) {
    const out = [];
    for (const p of points) {
      if (out.length && p.x === out.at(-1).x && p.y === out.at(-1).y) continue;
      while (out.length > 1) {
        const a = out.at(-2), b = out.at(-1);
        if ((a.x === b.x && b.x === p.x || a.y === b.y && b.y === p.y) && (b.x-a.x)*(p.x-b.x)+(b.y-a.y)*(p.y-b.y) >= 0) out.pop();else break;
      }
      out.push(p);
    }
    return out;
  }
  const midpoint = (a,b) => ({x:(a.x+b.x)/2,y:(a.y+b.y)/2});
  const direction = (a,b) => {const length=Math.hypot(b.x-a.x,b.y-a.y)||1;return {x:(b.x-a.x)/length,y:(b.y-a.y)/length};};
  function curveClear(curve, rectangles, depth=0) {
    // Subdivide the actual Bezier hull: checking its old routing polyline can
    // miss a curve cutting through a node between the guide points.
    const minX=Math.min(...curve.map(p=>p.x)),maxX=Math.max(...curve.map(p=>p.x)),minY=Math.min(...curve.map(p=>p.y)),maxY=Math.max(...curve.map(p=>p.y));
    const hits=rectangles.filter(r=>maxX>r.x+.01&&minX<r.x+r.width-.01&&maxY>r.y+.01&&minY<r.y+r.height-.01);
    if(!hits.length)return true;
    if(hits.some(r=>inside(curve[0],r)||inside(curve[3],r))||depth===12)return false;
    const [a,b,c,d]=curve,ab=midpoint(a,b),bc=midpoint(b,c),cd=midpoint(c,d),abc=midpoint(ab,bc),bcd=midpoint(bc,cd),center=midpoint(abc,bcd);
    return curveClear([a,ab,abc,center],hits,depth+1)&&curveClear([center,bcd,cd,d],hits,depth+1);
  }
  function curveResult(curves, guidePoints) {
    const points=[curves[0][0]];let d=`M ${points[0].x} ${points[0].y}`;
    for(const [a,b,c,end] of curves){
      d+=` C ${b.x} ${b.y}, ${c.x} ${c.y}, ${end.x} ${end.y}`;
      for(let i=1;i<=32;i++){const t=i/32,s=1-t;points.push({x:s*s*s*a.x+3*s*s*t*b.x+3*s*t*t*c.x+t*t*t*end.x,y:s*s*s*a.y+3*s*s*t*b.y+3*s*t*t*c.y+t*t*t*end.y});}
    }
    return {d,points,guidePoints:guidePoints||points,blocked:false};
  }
  function spline(points, rectangles, via) {
    const p=simplify(points);
    if(p.length<2)return null;
    // A saved handle is an interpolation point, even when it lies on a straight
    // guide segment and would normally disappear during simplification.
    if(via&&!p.some(q=>q.x===via.x&&q.y===via.y)){
      const i=p.findIndex((b,i)=>i&&Math.abs(Math.hypot(via.x-p[i-1].x,via.y-p[i-1].y)+Math.hypot(b.x-via.x,b.y-via.y)-Math.hypot(b.x-p[i-1].x,b.y-p[i-1].y))<.01);
      if(i>0)p.splice(i,0,via);
    }
    const tangent=i=>{
      if(i===0||i===p.length-1)return {x:1,y:0};
      const a=direction(p[i-1],p[i]),b=direction(p[i],p[i+1]),length=Math.hypot(a.x+b.x,a.y+b.y);
      return length>.01?{x:(a.x+b.x)/length,y:(a.y+b.y)/length}:a;
    };
    const knot=i=>({point:p[i],tangent:tangent(i),at:i});
    const anchors=[knot(0),knot(p.length-1)];
    // Mid-leg anchors spread the turns into broad curves, instead of drawing
    // long right-angle legs with a fixed, tiny corner radius.
    for(let i=1;i<p.length-2;i++)anchors.push({point:midpoint(p[i],p[i+1]),tangent:direction(p[i],p[i+1]),at:i+.5});
    if(via){const i=p.findIndex(q=>q.x===via.x&&q.y===via.y);if(i>0&&i<p.length-1)anchors.push(knot(i));}
    anchors.sort((a,b)=>a.at-b.at);
    const fit=(a,b)=>{
      const dx=b.point.x-a.point.x,dy=b.point.y-a.point.y,length=Math.hypot(dx,dy);
      const handle=t=>Math.min(length*.45,Math.max(length*.15,Math.abs(dx*t.x+dy*t.y)*.8));
      for(const scale of [1,.7,.4,.2,.1,.04,.01,.001]){
        const h1=handle(a.tangent)*scale,h2=handle(b.tangent)*scale;
        const curve=[a.point,{x:a.point.x+a.tangent.x*h1,y:a.point.y+a.tangent.y*h1},{x:b.point.x-b.tangent.x*h2,y:b.point.y-b.tangent.y*h2},b.point];
        if(curveClear(curve,rectangles))return curve;
      }
      return null;
    };
    const curves=[];
    for(let i=1;i<anchors.length;i++){
      const a=anchors[i-1],b=anchors[i],direct=fit(a,b);
      if(direct){curves.push(direct);continue;}
      // A tight corridor may need its original corner as an extra knot. Its
      // shared tangent keeps the two curves smooth while their handles shorten.
      const local=[a,...p.map((_,i)=>i).filter(i=>i>a.at&&i<b.at).map(knot),b];
      for(let j=1;j<local.length;j++){const curve=fit(local[j-1],local[j]);if(!curve)return null;curves.push(curve);}
    }
    return curveResult(curves,p);
  }
  function orthogonal(start, end, rectangles, occupied, lane) {
    if (rectangles.some(r => inside(start,r) || inside(end,r))) return null;
    // Bound coordinate-grid size for large scenes while still checking every
    // obstacle. A failed route remains visible and is explicitly marked.
    const relevant = rectangles.slice().sort((a,b) => Math.abs(a.x-start.x)+Math.abs(a.y-start.y)-Math.abs(b.x-start.x)-Math.abs(b.y-start.y)).slice(0,55);
    const xs = [...new Set([start.x,end.x,(start.x+end.x)/2+lane,...relevant.flatMap(r=>[r.x,r.x+r.width])])].sort((a,b)=>a-b);
    const ys = [...new Set([start.y,end.y,(start.y+end.y)/2+lane,...relevant.flatMap(r=>[r.y,r.y+r.height])])].sort((a,b)=>a-b);
    const key = (x,y,axis) => `${x},${y},${axis}`, queue = new Heap(), costs = new Map(), previous = new Map();
    const sx=xs.indexOf(start.x), sy=ys.indexOf(start.y), ex=xs.indexOf(end.x), ey=ys.indexOf(end.y);
    const first=key(sx,sy,0);costs.set(first,0);queue.push({x:sx,y:sy,axis:0,key:first,cost:0,score:Math.abs(start.x-end.x)+Math.abs(start.y-end.y)});
    const visibility = new Map(); let steps = 0, last;
    while (queue.items.length && steps++ < 35000) {
      const c=queue.pop();if (c.cost !== costs.get(c.key)) continue;
      if (c.x===ex && c.y===ey) {last=c;break;}
      for (const [dx,dy,axis] of [[1,0,0],[-1,0,0],[0,1,1],[0,-1,1]]) {
        const x=c.x+dx,y=c.y+dy;if(x<0||y<0||x>=xs.length||y>=ys.length)continue;
        const a={x:xs[c.x],y:ys[c.y]},b={x:xs[x],y:ys[y]}, vk=`${c.x},${c.y}:${x},${y}`;
        if (!visibility.has(vk)) visibility.set(vk, !rectangles.some(r=>intersects(a,b,r)));
        if (!visibility.get(vk)) continue;
        let penalty=0;
        for(const segment of occupied) {
          const [u,v]=segment;
          if(a.y===b.y&&u.y===v.y&&Math.abs(a.y-u.y)<5) penalty+=Math.max(0,Math.min(Math.max(a.x,b.x),Math.max(u.x,v.x))-Math.max(Math.min(a.x,b.x),Math.min(u.x,v.x)))*2;
          else if(a.x===b.x&&u.x===v.x&&Math.abs(a.x-u.x)<5) penalty+=Math.max(0,Math.min(Math.max(a.y,b.y),Math.max(u.y,v.y))-Math.max(Math.min(a.y,b.y),Math.min(u.y,v.y)))*2;
        }
        const cost=c.cost+Math.abs(a.x-b.x)+Math.abs(a.y-b.y)+(axis!==c.axis?22:0)+penalty,k=key(x,y,axis);
        if(cost>=(costs.get(k)??Infinity))continue;
        costs.set(k,cost);previous.set(k,c);queue.push({x,y,axis,key:k,cost,score:cost+Math.abs(b.x-end.x)+Math.abs(b.y-end.y)});
      }
    }
    if(!last)return null;
    const points=[];while(last){points.push({x:xs[last.x],y:ys[last.y]});last=previous.get(last.key);}return points.reverse();
  }
  function route(a, b, rectangles, options = {}) {
    // The visible pin center sits half a pixel inside the card's border. Allow
    // that terminal contact without exempting the rest of either card's body.
    rectangles=rectangles.map(r=>{
      let left=r.x,right=r.x+r.width;
      if(r.key===options.from&&Math.abs(right-a.x)<1)right=Math.min(right,a.x);
      if(r.key===options.to&&Math.abs(left-b.x)<1)left=Math.max(left,b.x);
      return {...r,x:left,width:right-left};
    });
    const clearance=Math.min(options.clearance??(16+(options.lane||0)%7*5),b.x>a.x?Math.max(8,(b.x-a.x-12)/3):Infinity);
    const boxes=rectangles.map(r=>({...r,x:r.x-clearance,y:r.y-clearance,width:r.width+2*clearance,height:r.height+2*clearance}));
    const occupied=options.occupied||[], via=options.via;
    const bend=Math.max(28,(b.x-a.x)*.45);
    // Use a Blueprint-like spline for an unobstructed forward connection.
    if(!via && b.x-a.x>50) {
      const curve=[a,{x:a.x+bend,y:a.y},{x:b.x-bend,y:b.y},b];
      const obstacles=rectangles.map(r=>r.key===options.from||r.key===options.to?r:{...r,x:r.x-4,y:r.y-4,width:r.width+8,height:r.height+8});
      if(curveClear(curve,obstacles))return curveResult([curve]);
    }
    const start={x:a.x+clearance+2,y:a.y},end={x:b.x-clearance-2,y:b.y};
    const stops=[start,...(via?[via]:[]),end],points=[a],legOccupied=occupied.slice();let blocked=false;
    if(rectangles.some(r=>r.key!==options.from&&intersects(a,start,r)||r.key!==options.to&&intersects(end,b,r))) blocked=true;
    for(let i=1;i<stops.length&&!blocked;i++) {
      const part=orthogonal(stops[i-1],stops[i],boxes,legOccupied,((options.lane||0)%5-2)*6);
      if(!part){blocked=true;break;}points.push(...part);
      for(let j=1;j<part.length;j++)legOccupied.push([part[j-1],part[j]]);
    }
    points.push(b);
    const smooth=blocked?null:spline(points,rectangles,via);
    if(smooth)return smooth;
    if(clearance>8)return route(a,b,rectangles,{...options,clearance:Math.min(14,clearance/2)});
    return {d:`M ${a.x} ${a.y} C ${a.x+45} ${a.y}, ${b.x-45} ${b.y}, ${b.x} ${b.y}`,points:[a,b],blocked:true};
  }
  const api={contextOwner,scopeChoices,scopedData,arrange,route,intersects,inside};
  if(typeof module==='object'&&module.exports)module.exports=api;else root.Blueprint=api;
})(typeof globalThis==='object'?globalThis:this);
