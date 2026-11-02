"use client";
import { createContext, useContext, useEffect, useRef, useState, useCallback, type Dispatch, type SetStateAction } from "react";
import { api, json, newProject, type Project, type Provider, type Job, type Character, type Settings, type Hardware, type Health, type View, activeJob } from "@/lib/studio";
type Toast={message:string;error:boolean};
type Studio={ view:View;setView:Dispatch<SetStateAction<View>>; project:Project;setProject:Dispatch<SetStateAction<Project>>; projects:Project[];providers:Provider[];jobs:Job[];characters:Character[];settings:Settings;hardware:Hardware;health:Health;loading:boolean;offline:boolean;busy:string;toast:Toast|null;notify:(message:string,error?:boolean)=>void;refresh:()=>Promise<void>;save:()=>Promise<Project>;generate:(sceneId?:string)=>Promise<void>;open:(project:Project,view?:View)=>void;fresh:(mode?:Project["mode"])=>void;run:<T>(name:string,action:()=>Promise<T>)=>Promise<T|undefined>;dirty:boolean;setSettings:Dispatch<SetStateAction<Settings>>; };
const StudioContext=createContext<Studio|null>(null);
export const useStudio=()=>{const value=useContext(StudioContext);if(!value)throw new Error("Studio context missing");return value};
export function StudioProvider({children}:{children:React.ReactNode}){
 const [view,setView]=useState<View>("dashboard");const [project,rawSetProject]=useState<Project>(newProject);const [projects,setProjects]=useState<Project[]>([]);const [providers,setProviders]=useState<Provider[]>([]);const [jobs,setJobs]=useState<Job[]>([]);const [characters,setCharacters]=useState<Character[]>([]);const [settings,setSettings]=useState<Settings>({});const [hardware,setHardware]=useState<Hardware>({});const [health,setHealth]=useState<Health>({});const [loading,setLoading]=useState(true);const [offline,setOffline]=useState(false);const [busy,setBusy]=useState("");const [toast,setToast]=useState<Toast|null>(null);const [dirty,setDirty]=useState(false);const dirtyRef=useRef(false);const projectRef=useRef(project);const completedRef=useRef("");
 const setProject:Dispatch<SetStateAction<Project>>=update=>{dirtyRef.current=true;setDirty(true);rawSetProject(update)};
 useEffect(()=>{projectRef.current=project},[project]);
 const notify=useCallback((message:string,error=false)=>{setToast({message,error})},[]);
 useEffect(()=>{if(!toast)return;const timer=setTimeout(()=>setToast(null),6500);return()=>clearTimeout(timer)},[toast]);
 const refresh=useCallback(async()=>{const paths=["/health","/projects","/providers","/jobs","/characters","/settings","/hardware"];
 const values=await Promise.allSettled(paths.map(path=>api<unknown>(path)));
 const [h,p,v,j,c,s,w]=values;
 if(h.status==="fulfilled"){setHealth(h.value as Health);setOffline(false)}else setOffline(true);
 if(p.status==="fulfilled")setProjects(p.value as Project[]);
 if(v.status==="fulfilled")setProviders(v.value as Provider[]);
 if(j.status==="fulfilled"){
  const next=j.value as Job[];setJobs(next);const current=projectRef.current;const finished=next.find(job=>job.project_id===current.id&&job.status==="completed");
  if(finished&&finished.updated_at!==completedRef.current&&!dirtyRef.current){completedRef.current=finished.updated_at;try{rawSetProject(await api<Project>(`/projects/${current.id}`))}catch{}}
 }
 if(c.status==="fulfilled")setCharacters(c.value as Character[]);
 if(s.status==="fulfilled")setSettings(s.value as Settings);
 if(w.status==="fulfilled")setHardware(w.value as Hardware);
 setLoading(false);
 },[]);
 useEffect(()=>{void refresh();const timer=setInterval(()=>void refresh(),7000);return()=>clearInterval(timer)},[refresh]);
 useEffect(()=>{const warn=(event:BeforeUnloadEvent)=>{if(dirty){event.preventDefault();event.returnValue=""}};window.addEventListener("beforeunload",warn);return()=>window.removeEventListener("beforeunload",warn)},[dirty]);
 async function run<T>(name:string,action:()=>Promise<T>):Promise<T|undefined>{setBusy(name);try{return await action()}catch(e){notify((e as Error).message,true);return undefined}finally{setBusy("")}}
 async function save(){const {id,created_at,updated_at,output_asset_id,...payload}=projectRef.current;void created_at;void updated_at;void output_asset_id;const result=await api<Project>(id?`/projects/${id}`:"/projects",{method:id?"PATCH":"POST",body:json(payload)});rawSetProject(result);projectRef.current=result;dirtyRef.current=false;setDirty(false);await refresh();return result;}
 async function generate(sceneId?:string){await run("generate",async()=>{const saved=await save();await api(`/projects/${saved.id}/generate`,{method:"POST",body:json(sceneId?{scene_id:sceneId}:{})});notify(sceneId?"Scene regeneration queued.":"Your production job is queued.");await refresh()})}
 function open(next:Project,nextView:View="generator"){if(dirtyRef.current&&!window.confirm("Discard the unsaved changes in the current project?"))return;rawSetProject(next);projectRef.current=next;dirtyRef.current=false;setDirty(false);setView(nextView);completedRef.current="";}
 function fresh(mode:Project["mode"]="text"){open({...newProject(),mode},mode==="script"?"script":"generator")}
 return <StudioContext.Provider value={{view,setView,project,setProject,projects,providers,jobs,characters,settings,setSettings,hardware,health,loading,offline,busy,toast,notify,refresh,save,generate,open,fresh,run,dirty}}>{children}</StudioContext.Provider>;
}
