# ============================================================================
#  K1 Finder (WPF) - Phase 2: Discover tab end-to-end
#  Refined-hybrid shell + the first fully-wired tab. Proves the load-bearing
#  pattern: an MTA scan runspace (copied VERBATIM from the live K1Finder.ps1,
#  pinned in _redesign/BASELINE.md) writes only the synchronized $sync hashtable;
#  a 250 ms DispatcherTimer on the UI thread drains it into a data-bound ListView
#  and a colored FlowDocument console.
#
#  Coexists with the live WinForms app - deploys nothing, no robot writes.
#  Launch via "K1 Finder (WPF).bat" (passes -ExecutionPolicy Bypass -STA).
#  Porting-fidelity rules (PLAN.md section 8): #2 drain on the UI thread; #7 XAML
#  wrapped in try/catch; #8 runspace MTA + Window STA.
# ============================================================================

Set-StrictMode -Off
$ErrorActionPreference = 'Stop'

# ---- STA guard (rule #8) ---------------------------------------------------
if ([System.Threading.Thread]::CurrentThread.GetApartmentState() -ne 'STA') {
    $self = $MyInvocation.MyCommand.Path
    if ($self) { & powershell.exe -NoProfile -ExecutionPolicy Bypass -STA -File $self; return }
    Write-Error 'K1 Finder (WPF) must run on an STA thread (use the .bat launcher).'; return
}

$SCRIPT_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path

try {
    Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase, System.Xaml
} catch {
    $m = "Failed to load WPF assemblies: $($_.Exception.Message)"
    if ($env:K1_WPF_SMOKE) { Write-Output "SMOKE FAIL - $m"; exit 1 }
    Write-Error $m; return
}

# Real CLR row type for the results ListView (WPF binding to PSCustomObject is
# unreliable; a CLR class with real properties binds cleanly - audit rec).
if (-not ('K1Row' -as [type])) {
    Add-Type -TypeDefinition @'
public class K1Row {
    public string Confidence { get; set; }
    public string IP { get; set; }
    public string Hostname { get; set; }
    public string Banner { get; set; }
    public string Why { get; set; }
    public int Score { get; set; }
}
'@
}

# ============================================================================
#  Config + backend - copied VERBATIM from the pinned live K1Finder.ps1
#  (lines 26-28, 31, 88-181). Do not edit: behavior must be byte-identical.
# ============================================================================
$K1_DEFAULT_IP    = '192.168.1.81'
$K1_SSH_USER      = 'booster'
$K1_SSH_PASS      = '123456'
$LAST_TARGET_FILE = Join-Path $SCRIPT_DIR 'last_target.txt'
$script:RobotIP   = $K1_DEFAULT_IP

# ---- Reachability helper (verbatim) ----------------------------------------
function Test-K1Reachable {
    param([string]$ip)
    $result = [ordered]@{ Ping = $false; SSH = $false; Banner = ''; Hostname = '' }
    try { $result.Ping = (Test-Connection -ComputerName $ip -Count 1 -Quiet -ErrorAction SilentlyContinue) } catch { }
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $ar = $c.BeginConnect($ip, 22, $null, $null)
        if ($ar.AsyncWaitHandle.WaitOne(1200)) {
            $c.EndConnect($ar)
            if ($c.Connected) {
                $result.SSH = $true
                try {
                    $stream = $c.GetStream(); $stream.ReadTimeout = 800
                    Start-Sleep -Milliseconds 150
                    $buf = New-Object byte[] 256
                    $n = $stream.Read($buf, 0, 256)
                    if ($n -gt 0) { $result.Banner = ([System.Text.Encoding]::ASCII.GetString($buf,0,$n)).Trim() }
                } catch { }
            }
        }
        $c.Close()
    } catch { }
    try { $result.Hostname = [System.Net.Dns]::GetHostEntry($ip).HostName } catch { }
    $result
}

# ---- Discover scan state + worker (verbatim) -------------------------------
$sync = [hashtable]::Synchronized(@{
    Running=$false; Cancel=$false; Progress=0; Total=0
    Results=(New-Object System.Collections.ArrayList); Log=(New-Object System.Collections.Queue)
    Subnets=''; Done=$false; PS=$null; Handle=$null
})
$scanScript = {
    function Get-LocalSubnets {
        $list = @()
        try {
            $ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | Where-Object {
                $_.IPAddress -ne '127.0.0.1' -and $_.IPAddress -notlike '169.254.*' }
            foreach ($i in $ips) {
                $oct = $i.IPAddress.Split('.')
                if ($oct.Count -eq 4) {
                    $list += [pscustomobject]@{ Base="$($oct[0]).$($oct[1]).$($oct[2])."; Self=$i.IPAddress; Iface=$i.InterfaceAlias }
                }
            }
        } catch { }
        $list
    }
    function Score-Candidate($ip,$banner,$hostname) {
        $score=0;$reasons=@()
        if ($ip -eq '192.168.10.102'){$score+=50;$reasons+='Default K1 wired IP (.102)'}
        if ($ip -like '192.168.10.*'){$score+=15;$reasons+='On K1 default subnet 192.168.10.x'}
        if ($banner -match 'SSH'){$score+=10;$reasons+='SSH (port 22) open'}
        if ($banner -match 'Ubuntu|Debian'){$score+=15;$reasons+='Ubuntu/Debian host (K1 runs Ubuntu)'}
        if ($hostname -match 'booster|k1|robot'){$score+=45;$reasons+='Hostname matches Booster/K1/robot'}
        $label='Low'; if($score -ge 50){$label='High'}elseif($score -ge 25){$label='Medium'}
        [pscustomobject]@{Score=$score;Label=$label;Why=($reasons -join '; ')}
    }
    try {
        $sync.Log.Enqueue('Detecting local network interfaces...')
        $subnets = Get-LocalSubnets
        $bases=@{}; $sumTxt=@()
        foreach($s in $subnets){ $bases[$s.Base]=$true; $sumTxt += ("{0}0/24 (this PC: {1}, {2})" -f $s.Base,$s.Self,$s.Iface) }
        $sync.Subnets = ($sumTxt -join '   |   ')
        if ($bases.Count -eq 0){ $sync.Log.Enqueue('No usable IPv4 subnet found.'); $sync.Done=$true; return }
        $sync.Log.Enqueue(("Found {0} subnet(s)." -f $bases.Count))
        $targets = New-Object System.Collections.Generic.List[string]
        foreach($b in $bases.Keys){ for($h=1;$h -le 254;$h++){ $targets.Add($b+$h) } }
        $sync.Total=$targets.Count
        $sync.Log.Enqueue(("Scanning {0} addresses for SSH/port 22 ..." -f $targets.Count))
        $batchSize=80; $timeoutMs=500
        for($start=0;$start -lt $targets.Count;$start+=$batchSize){
            if($sync.Cancel){ $sync.Log.Enqueue('Scan cancelled.'); break }
            $end=[Math]::Min($start+$batchSize,$targets.Count)-1; $batch=$targets[$start..$end]; $pending=@{}
            foreach($ip in $batch){ try{ $c=New-Object System.Net.Sockets.TcpClient; $ar=$c.BeginConnect($ip,22,$null,$null); $pending[$ip]=@{Client=$c;Async=$ar} }catch{} }
            Start-Sleep -Milliseconds $timeoutMs
            foreach($ip in $pending.Keys){
                $c=$pending[$ip].Client; $ar=$pending[$ip].Async; $open=$false
                try{ if($ar.AsyncWaitHandle.WaitOne(0)){ $c.EndConnect($ar); if($c.Connected){$open=$true} } }catch{}
                if($open){
                    $banner=''
                    try{ $st=$c.GetStream(); $st.ReadTimeout=600; Start-Sleep -Milliseconds 120; $buf=New-Object byte[] 256; $n=$st.Read($buf,0,256); if($n -gt 0){$banner=([System.Text.Encoding]::ASCII.GetString($buf,0,$n)).Trim()} }catch{}
                    $hn=''; try{$hn=[System.Net.Dns]::GetHostEntry($ip).HostName}catch{}
                    $sc=Score-Candidate $ip $banner $hn
                    [void]$sync.Results.Add([pscustomobject]@{Score=$sc.Score;Label=$sc.Label;IP=$ip;Hostname=$hn;Banner=$banner;Why=$sc.Why})
                    $sync.Log.Enqueue(("  [{0}] {1}  {2}  {3}" -f $sc.Label,$ip,$hn,$banner))
                }
                try{$c.Close()}catch{}
            }
            $sync.Progress=$end+1
        }
        if(-not $sync.Cancel){ $sync.Log.Enqueue(("Scan complete. {0} host(s) found." -f $sync.Results.Count)) }
    } catch { $sync.Log.Enqueue('Scan error: '+$_.Exception.Message) }
    finally { $sync.Done=$true }
}

# ============================================================================
#  XAML - refined-hybrid theme + shell. {StaticResource} braces are XAML.
# ============================================================================
$xaml = @'
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="K1 Finder - Booster K1 Discovery, SSH, Live View &amp; Control"
        Width="980" Height="720" MinWidth="760" MinHeight="600"
        WindowStartupLocation="CenterScreen"
        FontFamily="Segoe UI" FontSize="13" Background="#FFFFFF">

  <Window.Resources>
    <SolidColorBrush x:Key="Accent"       Color="#0078D7"/>
    <SolidColorBrush x:Key="AccentHover"  Color="#1B86E0"/>
    <SolidColorBrush x:Key="Ok"           Color="#107C10"/>
    <SolidColorBrush x:Key="Warn"         Color="#CA7C00"/>
    <SolidColorBrush x:Key="Danger"       Color="#C42B1C"/>
    <SolidColorBrush x:Key="DangerHover"  Color="#A32D2D"/>
    <SolidColorBrush x:Key="HeaderDark"   Color="#202225"/>
    <SolidColorBrush x:Key="Surface"      Color="#FFFFFF"/>
    <SolidColorBrush x:Key="SurfaceAlt"   Color="#F3F4F6"/>
    <SolidColorBrush x:Key="Stroke"       Color="#E1E3E6"/>
    <SolidColorBrush x:Key="TextPrimary"  Color="#202225"/>
    <SolidColorBrush x:Key="TextMuted"    Color="#6B7075"/>
    <SolidColorBrush x:Key="Neutral"      Color="#5F6368"/>

    <Style x:Key="BtnPrimary" TargetType="Button">
      <Setter Property="Foreground" Value="White"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="SnapsToDevicePixels" Value="True"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border x:Name="b" Background="{StaticResource Accent}" CornerRadius="4" Padding="14,7">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True"><Setter TargetName="b" Property="Background" Value="{StaticResource AccentHover}"/></Trigger>
              <Trigger Property="IsEnabled" Value="False"><Setter TargetName="b" Property="Background" Value="#9CC4E8"/></Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <Style x:Key="BtnSecondary" TargetType="Button">
      <Setter Property="Foreground" Value="{StaticResource TextPrimary}"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="SnapsToDevicePixels" Value="True"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border x:Name="b" Background="{StaticResource Surface}" BorderBrush="{StaticResource Stroke}" BorderThickness="1" CornerRadius="4" Padding="13,6">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True"><Setter TargetName="b" Property="Background" Value="{StaticResource SurfaceAlt}"/></Trigger>
              <Trigger Property="IsEnabled" Value="False"><Setter Property="Foreground" Value="#A8ACB1"/></Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <Style x:Key="BtnDanger" TargetType="Button">
      <Setter Property="Foreground" Value="White"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="SnapsToDevicePixels" Value="True"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border x:Name="b" Background="{StaticResource Danger}" CornerRadius="4" Padding="14,7">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True"><Setter TargetName="b" Property="Background" Value="{StaticResource DangerHover}"/></Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <Style TargetType="TabItem">
      <Setter Property="Foreground" Value="{StaticResource TextMuted}"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="TabItem">
            <Border x:Name="bd" Background="Transparent" BorderBrush="Transparent" BorderThickness="0,0,0,2" Padding="14,10">
              <ContentPresenter ContentSource="Header" HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsSelected" Value="True">
                <Setter TargetName="bd" Property="BorderBrush" Value="{StaticResource Accent}"/>
                <Setter Property="Foreground" Value="{StaticResource TextPrimary}"/>
                <Setter Property="FontWeight" Value="SemiBold"/>
              </Trigger>
              <Trigger Property="IsMouseOver" Value="True"><Setter TargetName="bd" Property="Background" Value="{StaticResource SurfaceAlt}"/></Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <Style x:Key="Pill" TargetType="Border">
      <Setter Property="CornerRadius" Value="10"/>
      <Setter Property="Padding" Value="10,4"/>
    </Style>

    <!-- Confidence cell: color the label by value, no converter needed -->
    <Style x:Key="ConfCell" TargetType="TextBlock">
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Foreground" Value="{StaticResource Neutral}"/>
      <Style.Triggers>
        <DataTrigger Binding="{Binding Confidence}" Value="High"><Setter Property="Foreground" Value="{StaticResource Ok}"/></DataTrigger>
        <DataTrigger Binding="{Binding Confidence}" Value="Medium"><Setter Property="Foreground" Value="{StaticResource Warn}"/></DataTrigger>
      </Style.Triggers>
    </Style>
  </Window.Resources>

  <DockPanel LastChildFill="True">

    <Border DockPanel.Dock="Top" Background="{StaticResource HeaderDark}" Height="50">
      <Grid Margin="14,0">
        <StackPanel Orientation="Horizontal" VerticalAlignment="Center" HorizontalAlignment="Left">
          <TextBlock Text="Booster K1" Foreground="White" FontSize="14" FontWeight="SemiBold" VerticalAlignment="Center"/>
          <TextBlock Text=" &#183; Finder" Foreground="#9AA0A6" FontSize="14" VerticalAlignment="Center"/>
          <TextBlock x:Name="hdrIp" Text="192.168.1.81" Foreground="#9AA0A6" FontFamily="Consolas" FontSize="12" Margin="12,0,0,0" VerticalAlignment="Center"/>
        </StackPanel>
        <Border Style="{StaticResource Pill}" Background="#2A2D31" HorizontalAlignment="Right" VerticalAlignment="Center">
          <StackPanel Orientation="Horizontal" VerticalAlignment="Center">
            <Ellipse x:Name="hdrDot" Width="8" Height="8" Fill="#6B7075" VerticalAlignment="Center"/>
            <TextBlock x:Name="hdrConn" Text="not connected" Foreground="#C9CDD2" FontSize="12" Margin="6,0,0,0" VerticalAlignment="Center"/>
          </StackPanel>
        </Border>
      </Grid>
    </Border>

    <Border DockPanel.Dock="Bottom" Background="{StaticResource SurfaceAlt}" BorderBrush="{StaticResource Stroke}" BorderThickness="0,1,0,0" Height="26">
      <TextBlock x:Name="statusLbl" Text="Ready." Foreground="{StaticResource TextMuted}" FontSize="12" VerticalAlignment="Center" Margin="12,0"/>
    </Border>

    <TabControl x:Name="tabs" Background="{StaticResource Surface}" BorderThickness="0" Padding="0">

      <TabItem Header="Discover">
        <Grid x:Name="paneDiscover" Background="{StaticResource Surface}">
          <DockPanel Margin="12">

            <Border DockPanel.Dock="Top" BorderBrush="{StaticResource Stroke}" BorderThickness="1" CornerRadius="6" Padding="12" Margin="0,0,0,8">
              <StackPanel>
                <TextBlock x:Name="subnetLbl" Text="Subnets: (detected at scan time)" Foreground="{StaticResource TextMuted}" FontSize="12" Margin="0,0,0,8"/>
                <StackPanel Orientation="Horizontal" VerticalAlignment="Center">
                  <Button x:Name="scanBtn" Content="Scan for K1" Style="{StaticResource BtnPrimary}" Margin="0,0,8,0"/>
                  <Button x:Name="stopBtn" Content="Stop" Style="{StaticResource BtnSecondary}" IsEnabled="False" Margin="0,0,16,0"/>
                  <TextBlock Text="Or enter K1 IP:" Foreground="{StaticResource TextMuted}" VerticalAlignment="Center" Margin="0,0,8,0"/>
                  <TextBox x:Name="ipBox" Width="140" Height="28" FontFamily="Consolas" VerticalContentAlignment="Center" Text="192.168.1.81" Margin="0,0,8,0"/>
                  <Button x:Name="verifyBtn" Content="Verify" Style="{StaticResource BtnSecondary}" Margin="0,0,16,0"/>
                  <ProgressBar x:Name="progress" Width="180" Height="10" Minimum="0" VerticalAlignment="Center"/>
                </StackPanel>
              </StackPanel>
            </Border>

            <DockPanel DockPanel.Dock="Bottom" Margin="0,8,0,0">
              <StackPanel DockPanel.Dock="Top" Orientation="Horizontal" Margin="0,0,0,8">
                <Button x:Name="connectBtn" Content="Verify selected" Style="{StaticResource BtnSecondary}" Margin="0,0,8,0"/>
                <Button x:Name="useBtn" Content="Use this IP everywhere" Style="{StaticResource BtnSecondary}"/>
              </StackPanel>
              <RichTextBox x:Name="logBox" Height="170" IsReadOnly="True" FontFamily="Consolas" FontSize="12"
                           Background="#202225" Foreground="#DCDCDC" BorderThickness="0"
                           VerticalScrollBarVisibility="Auto" HorizontalScrollBarVisibility="Disabled"/>
            </DockPanel>

            <ListView x:Name="list" BorderBrush="{StaticResource Stroke}" BorderThickness="1">
              <ListView.View>
                <GridView>
                  <GridViewColumn Header="Confidence" Width="92">
                    <GridViewColumn.CellTemplate>
                      <DataTemplate><TextBlock Text="{Binding Confidence}" Style="{StaticResource ConfCell}"/></DataTemplate>
                    </GridViewColumn.CellTemplate>
                  </GridViewColumn>
                  <GridViewColumn Header="IP address" Width="130" DisplayMemberBinding="{Binding IP}"/>
                  <GridViewColumn Header="Hostname" Width="150" DisplayMemberBinding="{Binding Hostname}"/>
                  <GridViewColumn Header="SSH banner" Width="200" DisplayMemberBinding="{Binding Banner}"/>
                  <GridViewColumn Header="Why" Width="230" DisplayMemberBinding="{Binding Why}"/>
                </GridView>
              </ListView.View>
            </ListView>

          </DockPanel>
        </Grid>
      </TabItem>

      <TabItem Header="SSH &amp; Files">
        <Grid x:Name="paneSsh" Background="{StaticResource Surface}">
          <StackPanel Margin="16" VerticalAlignment="Top">
            <TextBlock Text="SSH &amp; Files" FontSize="18" FontWeight="SemiBold" Foreground="{StaticResource TextPrimary}"/>
            <TextBlock Text="Phase 4 - terminal, key install, scp upload." Foreground="{StaticResource TextMuted}" Margin="0,4,0,0"/>
          </StackPanel>
        </Grid>
      </TabItem>

      <TabItem Header="Live View">
        <Grid x:Name="paneLive" Background="{StaticResource Surface}">
          <StackPanel Margin="16" VerticalAlignment="Top">
            <TextBlock Text="Live View" FontSize="18" FontWeight="SemiBold" Foreground="{StaticResource TextPrimary}"/>
            <TextBlock Text="Phase 3 - frozen-BitmapImage video path." Foreground="{StaticResource TextMuted}" Margin="0,4,0,0"/>
          </StackPanel>
        </Grid>
      </TabItem>

      <TabItem Header="Control">
        <Grid x:Name="paneCtrl" Background="{StaticResource Surface}">
          <StackPanel Margin="16" VerticalAlignment="Top">
            <TextBlock Text="Control" FontSize="18" FontWeight="SemiBold" Foreground="{StaticResource TextPrimary}"/>
            <TextBlock Text="Phase 4 - command grid, ARM/STOP hierarchy, lockouts." Foreground="{StaticResource TextMuted}" Margin="0,4,0,0"/>
          </StackPanel>
        </Grid>
      </TabItem>

      <TabItem Header="Robot Files">
        <Grid x:Name="paneFiles" Background="{StaticResource Surface}">
          <StackPanel Margin="16" VerticalAlignment="Top">
            <TextBlock Text="Robot Files" FontSize="18" FontWeight="SemiBold" Foreground="{StaticResource TextPrimary}"/>
            <TextBlock Text="Phase 5 - TreeView, key paths, preview." Foreground="{StaticResource TextMuted}" Margin="0,4,0,0"/>
          </StackPanel>
        </Grid>
      </TabItem>

      <TabItem Header="Tracker">
        <Grid x:Name="paneTrack" Background="{StaticResource Surface}">
          <StackPanel Margin="16" VerticalAlignment="Top">
            <TextBlock Text="Tracker" FontSize="18" FontWeight="SemiBold" Foreground="{StaticResource TextPrimary}"/>
            <TextBlock Text="Phase 3 - the cockpit: sliders, big video, state badge." Foreground="{StaticResource TextMuted}" Margin="0,4,0,0"/>
          </StackPanel>
        </Grid>
      </TabItem>

    </TabControl>
  </DockPanel>
</Window>
'@

# ---- build the Window (rule #7) --------------------------------------------
try {
    [xml]$xamlDoc = $xaml
    $reader = New-Object System.Xml.XmlNodeReader $xamlDoc
    $window = [Windows.Markup.XamlReader]::Load($reader)
} catch {
    $m = "XAML failed to load: $($_.Exception.Message)"
    if ($env:K1_WPF_SMOKE) { Write-Output "SMOKE FAIL - $m"; exit 1 }
    try { [void][System.Windows.MessageBox]::Show($m, 'K1 Finder (WPF) - startup error') } catch { Write-Output $m }
    return
}

# ---- $UI element map -------------------------------------------------------
$UINames = @('tabs','statusLbl','hdrIp','hdrConn','hdrDot',
             'paneDiscover','paneSsh','paneLive','paneCtrl','paneFiles','paneTrack',
             'subnetLbl','scanBtn','stopBtn','ipBox','verifyBtn','progress','list','connectBtn','useBtn','logBox')
$UI = @{}
foreach ($n in $UINames) { $UI[$n] = $window.FindName($n) }

# ---- media brushes for the colored log -------------------------------------
function New-Brush([string]$hex) {
    $b = New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.ColorConverter]::ConvertFromString($hex))
    $b.Freeze(); $b
}
$accentBrush     = New-Brush '#4AA3FF'   # readable accent on the dark console
$greenBrush      = New-Brush '#9ECE6A'
$amberBrush      = New-Brush '#D8A657'
$redBrush        = New-Brush '#F07178'
$paleGreenBrush  = New-Brush '#A9DC76'
$logDefaultBrush = New-Brush '#DCDCDC'

# ---- log console (FlowDocument; UI thread only) ----------------------------
$logDoc = New-Object System.Windows.Documents.FlowDocument
$logDoc.PagePadding = [System.Windows.Thickness]::new(0)
$script:logPara = New-Object System.Windows.Documents.Paragraph
$script:logPara.Margin = [System.Windows.Thickness]::new(0)
$logDoc.Blocks.Add($script:logPara)
$UI.logBox.Document = $logDoc

function Add-LogTo($rtb, $para, [string]$msg, $brush) {
    if ($null -eq $brush) { $brush = $logDefaultBrush }
    foreach ($ln in ($msg -split "\r?\n")) {
        $run = New-Object System.Windows.Documents.Run($ln); $run.Foreground = $brush
        $para.Inlines.Add($run); $para.Inlines.Add((New-Object System.Windows.Documents.LineBreak))
    }
    $rtb.ScrollToEnd()
}
function Add-Log([string]$m, $c) { Add-LogTo $UI.logBox $script:logPara $m $c }

# ---- results collection bound to the ListView ------------------------------
$script:rows = New-Object 'System.Collections.ObjectModel.ObservableCollection[object]'
$UI.list.ItemsSource = $script:rows
$script:renderedCount = 0

function Get-SelectedIP { if ($UI.list.SelectedItem) { return $UI.list.SelectedItem.IP }; return $null }
function Set-RobotIP([string]$ip) { if (-not $ip) { return }; $script:RobotIP = $ip; $UI.ipBox.Text = $ip; $UI.hdrIp.Text = $ip }

# ---- Connect recipe (verbatim text) ----------------------------------------
function Get-ConnectRecipe([string]$ip) { @"
================  K1 CONNECTION RECIPE  ================
Target robot IP : $ip
1) SSH:  ssh $K1_SSH_USER@$ip   (password: $K1_SSH_PASS)
2) SDK over Fast-DDS connects by robot IP; on-robot loco iface = 127.0.0.1.
   Loco CLI: ~/Workspace/booster_robotics_sdk/build/b1_loco_example_client 127.0.0.1
3) Live camera topic: /boostercamera/head/rgb (sensor_msgs/Image).
=======================================================
"@ }

# ---- Verify (WPF: Mouse.OverrideCursor + WPF MessageBox) -------------------
function Do-Verify([string]$ip) {
    if (-not $ip) { [void][System.Windows.MessageBox]::Show('No IP.', 'K1 Finder', [System.Windows.MessageBoxButton]::OK, [System.Windows.MessageBoxImage]::Warning); return }
    $UI.statusLbl.Text = "Verifying $ip ..."; Add-Log ("--- Verifying {0} ---" -f $ip) $accentBrush
    [System.Windows.Input.Mouse]::OverrideCursor = [System.Windows.Input.Cursors]::Wait
    $r = Test-K1Reachable $ip
    [System.Windows.Input.Mouse]::OverrideCursor = $null
    Add-Log ("  Ping     : {0}" -f $(if ($r.Ping) { 'reachable' } else { 'no reply' }))
    Add-Log ("  SSH (22) : {0}" -f $(if ($r.SSH) { 'OPEN' } else { 'closed' }))
    if ($r.Banner) { Add-Log ("  Banner   : {0}" -f $r.Banner) }; if ($r.Hostname) { Add-Log ("  Hostname : {0}" -f $r.Hostname) }
    if ($r.SSH) {
        Set-Content -Path $LAST_TARGET_FILE -Value $ip -Encoding ASCII; Set-RobotIP $ip
        Add-Log (Get-ConnectRecipe $ip) $paleGreenBrush
        $UI.statusLbl.Text = "Reachable: $ip (SSH open). IP applied."
    } else {
        $UI.statusLbl.Text = "No SSH on $ip."
        [void][System.Windows.MessageBox]::Show("SSH not open on $ip. Is the K1 on this network and powered on?", 'Not reachable', [System.Windows.MessageBoxButton]::OK, [System.Windows.MessageBoxImage]::Warning)
    }
}

# ---- scan control (runspace launch is verbatim) ----------------------------
function Start-Scan {
    if ($sync.Running) { return }
    $script:rows.Clear(); $sync.Results.Clear(); $sync.Log.Clear(); $sync.Progress = 0; $sync.Total = 0
    $sync.Cancel = $false; $sync.Done = $false; $sync.Running = $true; $script:renderedCount = 0; $UI.progress.Value = 0
    $UI.scanBtn.IsEnabled = $false; $UI.stopBtn.IsEnabled = $true; $UI.statusLbl.Text = 'Scanning...'; Add-Log '--- Starting scan ---' $accentBrush
    $rs = [runspacefactory]::CreateRunspace(); $rs.ApartmentState = 'MTA'; $rs.Open(); $rs.SessionStateProxy.SetVariable('sync', $sync)
    $ps = [powershell]::Create(); $ps.Runspace = $rs; [void]$ps.AddScript($scanScript); $sync.PS = $ps; $sync.Handle = $ps.BeginInvoke()
}
function Stop-Scan { $sync.Cancel = $true; $UI.statusLbl.Text = 'Stopping...' }

# ---- Confidence color helper (parity with live, for any future code) -------
function Confidence-Color([string]$label) { switch ($label) { 'High' { $greenBrush } 'Medium' { $amberBrush } default { $logDefaultBrush } } }

# ============================================================================
#  DispatcherTimers (rule #2): UI-thread drains; runspaces only write $sync.
# ============================================================================
function New-UITimer([int]$ms, [scriptblock]$tick) {
    $t = New-Object System.Windows.Threading.DispatcherTimer
    $t.Interval = [TimeSpan]::FromMilliseconds($ms)
    $t.Add_Tick($tick)
    return $t
}

# Discover poll timer - ported from live $timer.Add_Tick (lines 1079-1098)
$timer = New-UITimer 250 {
    while ($sync.Log.Count -gt 0) { $line = $sync.Log.Dequeue(); if ($line) { Add-Log $line } }
    if ($sync.Subnets -and $UI.subnetLbl.Text -notmatch [regex]::Escape($sync.Subnets)) { $UI.subnetLbl.Text = 'Subnets: ' + $sync.Subnets }
    if ($sync.Total -gt 0) { $UI.progress.Maximum = $sync.Total; $UI.progress.Value = [Math]::Min($sync.Progress, $sync.Total) }
    while ($script:renderedCount -lt $sync.Results.Count) {
        $r = $sync.Results[$script:renderedCount]
        $row = New-Object K1Row -Property @{ Confidence = $r.Label; IP = $r.IP; Hostname = $r.Hostname; Banner = $r.Banner; Why = $r.Why; Score = $r.Score }
        $script:rows.Add($row); $script:renderedCount++
    }
    if ($sync.Done -and $sync.Running) {
        $sync.Running = $false; $UI.scanBtn.IsEnabled = $true; $UI.stopBtn.IsEnabled = $false; $UI.progress.Value = $UI.progress.Maximum
        if ($script:rows.Count -gt 0) {
            $sorted = @($script:rows | Sort-Object { -1 * $_.Score })
            $script:rows.Clear(); foreach ($it in $sorted) { $script:rows.Add($it) }
            $UI.list.SelectedIndex = 0; $top = $script:rows[0]
            if ($top.Confidence -eq 'High') { $UI.statusLbl.Text = ("Likely K1: {0} ({1})" -f $top.IP, $top.Why) } else { $UI.statusLbl.Text = ("Scan done. Best guess: {0}" -f $top.IP) }
        } else { $UI.statusLbl.Text = 'Scan done. No SSH hosts found.' }
        try { $sync.PS.EndInvoke($sync.Handle) } catch {}; try { $sync.PS.Runspace.Close(); $sync.PS.Dispose() } catch {}
    }
}
$mediaTimer = New-UITimer 40   { }   # Phase 3: video frames / follow log / watchdogs
$fpsTimer   = New-UITimer 1000 { }   # Phase 3: FPS heartbeat

# ---- wire events (ported from live lines 1445-1450) ------------------------
$UI.scanBtn.Add_Click({ Start-Scan })
$UI.stopBtn.Add_Click({ Stop-Scan })
$UI.verifyBtn.Add_Click({ Do-Verify ($UI.ipBox.Text.Trim()) })
$UI.connectBtn.Add_Click({ $ip = Get-SelectedIP; if (-not $ip) { $ip = $UI.ipBox.Text.Trim() }; Do-Verify $ip })
$UI.useBtn.Add_Click({ $ip = Get-SelectedIP; if (-not $ip) { $ip = $UI.ipBox.Text.Trim() }; if ($ip) { Set-RobotIP $ip; $UI.statusLbl.Text = "Applied $ip." } })
$UI.list.Add_MouseDoubleClick({ Do-Verify (Get-SelectedIP) })
$UI.list.Add_SelectionChanged({ $ip = Get-SelectedIP; if ($ip) { $UI.ipBox.Text = $ip } })

# ---- safety teardown (rule #6): Phase 4+ adds Stop-Tracker/Follow/Live ------
$window.Add_Closing({
    $sync.Cancel = $true
    try { $timer.Stop() } catch {}; try { $mediaTimer.Stop() } catch {}; try { $fpsTimer.Stop() } catch {}
    try { if ($sync.PS) { $sync.PS.Stop() } } catch {}
})

# ---- startup seeding (parity with Add_Shown) -------------------------------
$window.Add_Loaded({ Add-Log 'K1 Finder (WPF) ready. Scan or enter the K1 IP, then Verify.' $accentBrush })

$timer.Start(); $mediaTimer.Start(); $fpsTimer.Start()

# pre-fill last verified IP across the app
if (Test-Path $LAST_TARGET_FILE) { $last = (Get-Content $LAST_TARGET_FILE -ErrorAction SilentlyContinue | Select-Object -First 1); if ($last) { Set-RobotIP $last.Trim() } }

# ---- row-test mode: inject synthetic results, prove binding+color+sort, render ---
if ($env:K1_WPF_ROWTEST) {
    foreach ($x in @(
        @{Score=10;Label='Low';IP='192.168.10.7';Hostname='';Banner='SSH-2.0-dropbear_2019.78';Why='SSH (port 22) open'},
        @{Score=50;Label='High';IP='192.168.10.102';Hostname='booster-k1';Banner='SSH-2.0-OpenSSH_8.9p1 Ubuntu';Why='Default K1 wired IP (.102); Hostname matches Booster/K1/robot'},
        @{Score=25;Label='Medium';IP='192.168.10.50';Hostname='nas01.local';Banner='SSH-2.0-OpenSSH_7.6';Why='On K1 default subnet 192.168.10.x; SSH (22) open'}
    )) { [void]$sync.Results.Add([pscustomobject]$x) }
    $sync.Total = 254; $sync.Progress = 254; $sync.Subnets = '192.168.10.0/24 (this PC: 192.168.10.5, Ethernet)'; $sync.Running = $true; $sync.Done = $true
    $out = $env:K1_WPF_ROWTEST
    $window.WindowStartupLocation = 'Manual'; $window.Left = -10000; $window.Top = -10000; $window.ShowInTaskbar = $false
    $script:rt_done = $false
    $watch = New-Object System.Windows.Threading.DispatcherTimer
    $watch.Interval = [TimeSpan]::FromMilliseconds(200)
    $watch.Add_Tick({
        if (-not $script:rt_done -and -not $sync.Running -and $script:rows.Count -gt 0) {
            $window.UpdateLayout()
            $w = [int][math]::Ceiling($window.ActualWidth); $h = [int][math]::Ceiling($window.ActualHeight)
            $rtb = New-Object System.Windows.Media.Imaging.RenderTargetBitmap($w, $h, 96, 96, [System.Windows.Media.PixelFormats]::Pbgra32)
            $rtb.Render($window)
            $enc = New-Object System.Windows.Media.Imaging.PngBitmapEncoder
            $enc.Frames.Add([System.Windows.Media.Imaging.BitmapFrame]::Create($rtb))
            $fs = [System.IO.File]::Create($out); $enc.Save($fs); $fs.Close()
            $script:rt_done = $true; $watch.Stop(); $window.Close()
        }
    })
    $watch.Start()
    [void]$window.ShowDialog()
    Write-Output ("ROWTEST rows={0} selectedIP=[{1}] status=[{2}] -> {3}" -f $script:rows.Count, (Get-SelectedIP), $UI.statusLbl.Text, $out)
    exit 0
}

# ---- scan-test mode: drive a real scan end-to-end off-screen, then report ---
if ($env:K1_WPF_SCANTEST) {
    $window.WindowStartupLocation = 'Manual'; $window.Left = -10000; $window.Top = -10000; $window.ShowInTaskbar = $false
    $script:scanStart0 = [DateTime]::Now; $script:doneAt = $null
    $watch = New-Object System.Windows.Threading.DispatcherTimer
    $watch.Interval = [TimeSpan]::FromMilliseconds(250)
    $watch.Add_Tick({
        if ($sync.Done) {
            if ($null -eq $script:doneAt) { $script:doneAt = [DateTime]::Now }
            elseif (([DateTime]::Now - $script:doneAt).TotalMilliseconds -gt 800) { $watch.Stop(); $window.Close() }
        } elseif (([DateTime]::Now - $script:scanStart0).TotalSeconds -gt 50) { $watch.Stop(); $window.Close() }
    })
    $window.Add_ContentRendered({ Start-Scan })
    $watch.Start()
    [void]$window.ShowDialog()
    Write-Output ("SCANTEST done={0} results={1} rows={2} status=[{3}]" -f $sync.Done, $sync.Results.Count, $script:rows.Count, $UI.statusLbl.Text)
    exit 0
}

# ---- render mode: composite the shell to a PNG off-screen (dev/CI preview) --
if ($env:K1_WPF_RENDER) {
    $out = $env:K1_WPF_RENDER
    $window.WindowStartupLocation = 'Manual'
    $window.Left = -10000; $window.Top = -10000; $window.ShowInTaskbar = $false
    $rw = 980; $rh = 720
    if ($env:K1_WPF_W) { $rw = [int]$env:K1_WPF_W }; if ($env:K1_WPF_H) { $rh = [int]$env:K1_WPF_H }
    $window.Width = $rw; $window.Height = $rh
    $window.Show(); $window.UpdateLayout()
    $window.Dispatcher.Invoke([Action]{}, [System.Windows.Threading.DispatcherPriority]::Loaded)
    $w = [int][math]::Ceiling($window.ActualWidth); $h = [int][math]::Ceiling($window.ActualHeight)
    $rtb = New-Object System.Windows.Media.Imaging.RenderTargetBitmap($w, $h, 96, 96, [System.Windows.Media.PixelFormats]::Pbgra32)
    $rtb.Render($window)
    $enc = New-Object System.Windows.Media.Imaging.PngBitmapEncoder
    $enc.Frames.Add([System.Windows.Media.Imaging.BitmapFrame]::Create($rtb))
    $fs = [System.IO.File]::Create($out); $enc.Save($fs); $fs.Close(); $window.Close()
    Write-Output "RENDER OK - ${w}x${h} -> $out"; exit 0
}

# ---- smoke mode: build + verify without showing (headless CI) --------------
if ($env:K1_WPF_SMOKE) {
    $missing = @($UINames | Where-Object { -not $UI[$_] })
    if ($missing.Count) { Write-Output ("SMOKE FAIL - missing named elements: {0}" -f ($missing -join ', ')); $timer.Stop(); $mediaTimer.Stop(); $fpsTimer.Stop(); exit 1 }
    $found = @($UINames | Where-Object { $UI[$_] }).Count
    Write-Output ("SMOKE OK - window built; {0}/{1} named elements; ItemsSource bound; 3 DispatcherTimers running" -f $found, $UINames.Count)
    $timer.Stop(); $mediaTimer.Stop(); $fpsTimer.Stop(); exit 0
}

[void]$window.ShowDialog()
