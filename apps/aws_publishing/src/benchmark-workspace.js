import {BenchmarkLab as FixedBudgetLab} from './benchmark-lab.js';
import {AccuracyLab} from './benchmark-accuracy.js';
import {escapeHtml as h} from './plots.js';

export class BenchmarkLab{
  constructor(options){this.options=options;this.mode='fixed_budget';this.fixed=new FixedBudgetLab(options);this.accuracy=new AccuracyLab(options);}
  get active(){return this.mode==='accuracy_target'?this.accuracy:this.fixed;}
  reset(){this.fixed.reset();this.accuracy.reset();this.mode='fixed_budget';}
  leave(){this.fixed.leave();this.accuracy.leave();}
  async enter(){void this.accuracy.loadCapabilities();await this.active.enter();}
  async poll(){await this.active.poll();}
  captureFocus(){const node=globalThis.document?.activeElement;if(node?.dataset?.benchmarkMode)return {modeButton:node.dataset.benchmarkMode};return this.active.captureFocus();}
  view(){const enabled=this.accuracy.capabilities?.enabled;return '<section class="benchmark-mode-picker" aria-label="Comparison mode"><button type="button" data-benchmark-mode="fixed_budget" aria-pressed="'+(this.mode==='fixed_budget')+'">Fixed budget</button><button type="button" data-benchmark-mode="accuracy_target" aria-pressed="'+(this.mode==='accuracy_target')+'"'+(!enabled?' disabled':'')+'>Accuracy target</button>'+(!enabled?'<span class="muted">'+h(this.accuracy.capabilities?.reason??'Checking accuracy-mode availability…')+'</span>':'')+'</section>'+this.active.view();}
  mount(container,focus){
    this.active.mount(container,focus?.modeButton?null:focus);
    for(const mode of ['fixed_budget','accuracy_target']){
      const button=container.querySelector('[data-benchmark-mode="'+mode+'"]');if(!button)continue;
      button.onclick=async()=>{if(mode===this.mode||mode==='accuracy_target'&&!this.accuracy.capabilities?.enabled)return;this.active.leave();this.mode=mode;this.options.onChange();await this.active.enter();};
    }
    if(focus?.modeButton)container.querySelector('[data-benchmark-mode="'+focus.modeButton+'"]')?.focus();
  }
}
