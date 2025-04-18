#!/usr/bin/env python3

import os
import re
import sys
import subprocess
from typing import Optional
import paramiko
from colorama import init, Fore, Style
import time
import pyperclip
from abc import ABC, abstractmethod
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
import ssl

# Initialize colorama
init()

# 基礎配置器類別
class BaseConfigurator(ABC):
    # 初始化SSH連接
    def __init__(self):
        self.ssh_client = None
        self.si = None

    def test_connection(self, host, user, password):
        """測試 ESXi 連線"""
        try:
            context = ssl._create_unverified_context()
            si = SmartConnect(host=host, user=user, pwd=password, sslContext=context)
            if si:
                print(f"{Fore.GREEN}Connected to ESXi host successfully{Style.RESET_ALL}\n")
                return True, si
            return False, None
        except Exception as e:
            print(f"{Fore.RED}Failed to connect to ESXi host: {e}{Style.RESET_ALL}")
            return False, None

    def get_valid_input(self, prompt, input_type=str, default=None, min_value=None):
        """獲取有效的用戶輸入"""
        while True:
            value = input(prompt)
            if not value:  # 輸入不能是空白
                if default is not None:
                    return default
                continue
            try:
                result = input_type(value)
                if min_value is not None and result < min_value:
                    print(f"{Fore.YELLOW}Value must be at least {min_value}{Style.RESET_ALL}")
                    continue
                return result
            except ValueError:
                print(f"{Fore.YELLOW}Please enter a valid {input_type.__name__}{Style.RESET_ALL}")

    # 驗證IP地址格式
    def validate_ip(self, ip: str) -> bool:
        """Validate IP address format"""
        ip_pattern = r'^(\d{1,3}\.){3}\d{1,3}$'
        return re.match(ip_pattern, ip) is not None and \
               all(0 <= int(part) <= 255 for part in ip.split('.'))

    # 檢查IP是否可達
    def ping_check(self, ip: str) -> bool:
        """Check if IP is reachable"""
        try:
            param = '-n' if os.name == 'nt' else '-c'
            result = subprocess.run(['ping', param, '1', '-w', '1', ip], 
                                  capture_output=True, text=True)
            return result.returncode == 0
        except Exception:
            return False

    # 建立SSH連接
    def ssh_connect(self, hostname: str, username: str, password: str, key_path: Optional[str] = None):
        """Establish SSH connection"""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        try:
            # Set timeout to 5 seconds for connection attempt
            if key_path and os.path.exists(key_path):
                client.connect(hostname, username=username, key_filename=key_path, timeout=5)
            else:
                client.connect(hostname, username=username, password=password, timeout=5)
            return client
        except paramiko.AuthenticationException:
            print(f"{Fore.RED}Authentication failed. Please check username and password.{Style.RESET_ALL}")
            return None
        except paramiko.SSHException as e:
            print(f"{Fore.RED}SSH Connection Error: {str(e)}{Style.RESET_ALL}")
            return None
        except Exception as e:
            print(f"{Fore.RED}Connection Error: {str(e)}{Style.RESET_ALL}")
            return None

    # 主配置方法
    @abstractmethod
    def configure(self):
        """主配置方法，由子類別實作"""
        pass

    # 基礎網路配置方法
    @abstractmethod
    def configure_network(self, ssh_client, new_ip, subnet_mask, gateway):
        """基礎網路配置方法，由子類別實作"""
        raise NotImplementedError("Subclasses must implement this method")  

    def list_vms(self, si):
        """列出ESXi主機上的所有VM, 按創建時間排序"""
        try:
            content = si.RetrieveContent()
            container = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
            
            # 獲取VM並記錄創建時間
            vms_with_time = []
            for vm in container.view:
                # 使用VM的創建時間，如果沒有則使用最後修改時間
                create_time = vm.config.createDate or vm.config.modified
                vms_with_time.append((vm.name, create_time))
            
            container.Destroy()
            
            if not vms_with_time:
                print(f"{Fore.YELLOW}No VMs found on this host{Style.RESET_ALL}")
                return []
            
            # 按時間排序，最新的在最後
            vms_with_time.sort(key=lambda x: x[1])
            vm_names = [vm[0] for vm in vms_with_time]
                
            print(f"\nAvailable VMs on the target ESXi host (oldest to newest):")
            for i, name in enumerate(vm_names, 1):
                print(f"{i}) {name}")
            return vm_names
        except Exception as e:
            print(f"{Fore.RED}Error listing VMs: {e}{Style.RESET_ALL}")
            return []

# SUT配置器類別
class SUTConfigurator(BaseConfigurator):
    def __init__(self):
        super().__init__()
        self.username = "root"
        self.password = "Admin!23"
        self.default_submask = "255.255.252.0"
        self.default_gateway = "192.168.4.1"
        self.default_dns = "192.168.4.1"

    # SUT特定網路配置   
    def configure_network(self, ssh_client, new_ip, subnet_mask, gateway):
        """SUT specific network configuration"""
        try:
            cmd = f'esxcli network ip interface ipv4 set -i vmk0 -t static -I {new_ip} -N {subnet_mask} -g {gateway}'
            stdin, stdout, stderr = ssh_client.exec_command(cmd)
            if stderr.channel.recv_exit_status() == 0:
                return True
            else:
                print(f"{Fore.RED}Failed to set network configuration{Style.RESET_ALL}")
                return False
        except Exception as e:
            print(f"{Fore.RED}Error setting network configuration: {e}{Style.RESET_ALL}")
            return False

    # 配置ESXi DNS設定
    def configure_dns(self, ssh_client, primary_dns, hostname):
        """Configure ESXi DNS settings"""
        try:
            # 清除現有的 DNS servers
            stdin, stdout, stderr = ssh_client.exec_command('esxcli network ip dns server remove --all')
            if stderr.channel.recv_exit_status() != 0:
                print(f"{Fore.RED}Failed to clear existing DNS servers{Style.RESET_ALL}")
                return False

            # 設定 Primary DNS server IP
            stdin, stdout, stderr = ssh_client.exec_command(f'esxcli network ip dns server add -s {primary_dns}')
            if stderr.channel.recv_exit_status() != 0:
                print(f"{Fore.RED}Failed to set Primary DNS server{Style.RESET_ALL}")
                return False

            # 設定 DNS hostname (FQDN)
            stdin, stdout, stderr = ssh_client.exec_command(f'esxcli system hostname set --fqdn {hostname}')
            if stderr.channel.recv_exit_status() == 0:
                return True
            else:
                print(f"{Fore.RED}Failed to set DNS hostname{Style.RESET_ALL}")
                return False
        except Exception as e:
            print(f"{Fore.RED}Error setting DNS: {e}{Style.RESET_ALL}")
            return False

    # def enable_ssh_services(ssh_client):
#     """啟用SSH服務"""
#     try:
#         commands = [
#             'vim-cmd hostsvc/enable_ssh',
#             'vim-cmd hostsvc/start_ssh'
#         ]
#         for cmd in commands:
#             stdin, stdout, stderr = ssh_client.exec_command(cmd)
#             if stderr.channel.recv_exit_status() != 0:
#                 print(f"{Fore.RED}執行命令失敗: {cmd}{Style.RESET_ALL}")
#                 return False
#         print(f"{Fore.GREEN}SSH enabled successfully{Style.RESET_ALL}")
#         return True
#     except Exception as e:
#         print(f"{Fore.RED}啟用服務時發生錯誤: {e}{Style.RESET_ALL}")
#         return False


    # 配置防火牆
    def configure_firewall(self, ssh_client):
        """根據VMware版本設定防火牆"""
        try:
            # Get VMware version
            stdin, stdout, stderr = ssh_client.exec_command("vmware -r | awk -F ' ' '{print $3}' | cut -d '.' -f1")
            version = stdout.read().decode().strip()
            
            if version == '9':
                cmd = 'esxcli network firewall set --enabled false'
            elif version == '8':
                cmd = '''esxcli network firewall set --enabled false
                        esxcli system wbem set -e 0
                        esxcli system wbem set -e 1
                        esxcli hardware trustedboot get'''
            else:
                print(f"{Fore.YELLOW}Unsupported VMware version: {version}{Style.RESET_ALL}")
                return False

            stdin, stdout, stderr = ssh_client.exec_command(cmd)
            if stderr.channel.recv_exit_status() == 0:
                print(f"{Fore.GREEN}Firewall configured successfully{Style.RESET_ALL}")
                return True
            else:
                print(f"{Fore.RED}Failed to configure firewall{Style.RESET_ALL}")
                return False
        except Exception as e:
            print(f"{Fore.RED}Error configuring firewall: {e}{Style.RESET_ALL}")
            return False
    
    def enable_shell(self, ssh_client):
        """啟用 ESXi Shell"""
        try:
            commands = [
                'vim-cmd hostsvc/enable_esx_shell',
                'vim-cmd hostsvc/start_esx_shell'
            ]
            for cmd in commands:
                stdin, stdout, stderr = ssh_client.exec_command(cmd)
                if stderr.channel.recv_exit_status() != 0:
                    return False

            print(f"{Fore.GREEN}ESXi Shell enabled successfully{Style.RESET_ALL}")
            return True
        except Exception as e:
            print(f"{Fore.RED}Error enabling ESXi Shell: {e}{Style.RESET_ALL}")
            return False

    def display_system_info(self, ssh_client):
        """顯示 ESXi 主機系統資訊"""
        try:
            commands = {
                'Product Name': "esxcli hardware platform get | grep 'Product Name' | awk -F ': ' '{print $2}'",
                'OS version': "vmware -r",
                'Secure Boot state': "python3 /usr/lib/vmware/secureboot/bin/secureBoot.py -s",
                'BMC IP': "esxcli hardware ipmi bmc get | grep 'IPv4Address' | awk -F ': ' '{print $2}'",
                'OS IP': "esxcli network ip interface ipv4 get | awk 'NR==3 {print $2}'",
                'Submask': "esxcli network ip interface ipv4 get | awk 'NR==3 {print $3}'",
                'Gateway': "esxcli network ip interface ipv4 get | awk 'NR==3 {print $6}'",
                'DNS server': "esxcli network ip dns server list | awk -F ': ' '{print $2}'",
                'DNS hostname': "esxcli system hostname get | grep 'Fully Qualified Domain Name' | awk -F ': ' '{print $2}'"
            }

            for label, command in commands.items():
                stdin, stdout, stderr = ssh_client.exec_command(command)
                result = stdout.read().decode().strip()
                print(f"{label}: {Fore.YELLOW}{result}{Style.RESET_ALL}")
            
            return True
        except Exception as e:
            print(f"{Fore.RED}Error getting system information: {e}{Style.RESET_ALL}")
            return False

    def configure(self):
        print(
        f"""{Fore.CYAN}
 ___________________________________________________________________
 <Prerequisites>
 To move forward, make sure you've already completed the following:
    1. Installed VMware ESXi on SUT
    2. Enabled SSH access on SUT
    3. Obtained the DHCP IP address of SUT
 ___________________________________________________________________{Style.RESET_ALL}
    """
        )

        # 獲取 SUT IP
        while True:
            SUT_dhcp_ip = input("Enter SUT IP address: ").strip()
            if self.validate_ip(SUT_dhcp_ip):
                if self.ping_check(SUT_dhcp_ip):
                    break
                else:
                    print(f"{Fore.RED}Failed to ping IP{Style.RESET_ALL}")
            else:
                print(f"{Fore.YELLOW}Invalid IP format{Style.RESET_ALL}")

        # 建立 SSH 連接
        ssh_client = self.ssh_connect(SUT_dhcp_ip, self.username, self.password)
        if not ssh_client:
            return

        # 啟用 ESXi Shell
        print("\nEnabling ESXi Shell...")
        if not self.enable_shell(ssh_client):
            print(f"{Fore.RED}Failed to enable ESXi Shell{Style.RESET_ALL}")
            ssh_client.close()
            return

        # 顯示當前系統資訊
        print("\n\nGetting the current system information...")
        print("-----------------------------------------")
        self.display_system_info(ssh_client)

        # 獲取網路配置詳細資訊
        print("\n")
        while True:
            static_ip = input("Set a new IP: ").strip()
            if not self.validate_ip(static_ip):
                print(f"{Fore.YELLOW}Invalid IP format{Style.RESET_ALL}")
                continue
                
            if self.ping_check(static_ip):
                # 如果IP已被使用，則提示用戶選擇另一個IP
                print(f"{Fore.RED}IP {static_ip} is already in use. Please choose another IP.{Style.RESET_ALL}")
                continue
            break

        # 設定子網掩碼
        while True:
            subnet_mask = input(f"Set Subnet mask <press Enter to accept default {Fore.CYAN}{self.default_submask}{Style.RESET_ALL}>: ").strip()
            if subnet_mask == "":
                subnet_mask = self.default_submask
            if not self.validate_ip(subnet_mask):
                print(f"{Fore.YELLOW}Invalid netmask format{Style.RESET_ALL}")
                continue
            break

        # 設定預設閘道
        while True:
            gateway = input(f"Set default Gateway <press Enter to accept default {Fore.CYAN}{self.default_gateway}{Style.RESET_ALL}>: ").strip()
            if gateway == "":
                gateway = self.default_gateway
            if not self.validate_ip(gateway):
                print(f"{Fore.YELLOW}Invalid gateway format{Style.RESET_ALL}")
                continue
            break

        # 配置網路
        print("\nConfiguring IP settings...")
        if self.configure_network(ssh_client, static_ip, subnet_mask, gateway):
            print(f"{Fore.GREEN}Network configuration successful{Style.RESET_ALL}")
            ssh_client.close()
            time.sleep(5)  # Wait for network changes to take effect
            
            # 嘗試重新連接新IP
            ssh_client = self.ssh_connect(static_ip, self.username, self.password)
            if not ssh_client:
                print(f"{Fore.RED}Failed to reconnect with new IP{Style.RESET_ALL}")
                return

        # 獲取 DNS 配置詳細資訊
        print("\n")
        while True:
            primary_dns = input(f"Set primary DNS <press Enter to accept default {Fore.CYAN}{self.default_dns}{Style.RESET_ALL}>: ").strip()
            if primary_dns == "":
                primary_dns = self.default_dns
            if not self.validate_ip(primary_dns):
                print(f"{Fore.YELLOW}Invalid DNS IP format{Style.RESET_ALL}")
                continue
            break
        
        # 從 static_ip 提取最後一組數字作為預設DNS hostname名稱
        last_octet = static_ip.split('.')[-1]
        default_hostname = f"esxi{last_octet}"
        
        # 從用戶獲取 DNS hostname
        dns_hostname = input(f"Set DNS hostname <press Enter to accept default {Fore.CYAN}{default_hostname}{Style.RESET_ALL}>: ").strip()
        if dns_hostname == "":
            dns_hostname = default_hostname

        # 配置 DNS 和 DNS hostname
        print("\nConfiguring DNS settings...")
        if not self.configure_dns(ssh_client, primary_dns, dns_hostname):
            return
        print(f"{Fore.GREEN}DNS IP and hostname set successfully{Style.RESET_ALL}")

        # 配置防火牆
        print("\nConfiguring firewall...")
        if not self.configure_firewall(ssh_client):
            return

        # 顯示更新後的系統資訊
        print("\nGetting the updated system information...")
        print("-----------------------------------------")
        self.display_system_info(ssh_client)
        ssh_client.close()

        print(f"\n\n\n{Fore.GREEN}***************************************{Style.RESET_ALL}")
        print(f"{Fore.GREEN}All configurations have been completed!{Style.RESET_ALL}")
        print(f"\nRemember to create a new host {Fore.YELLOW}{dns_hostname}{Style.RESET_ALL} with IP {Fore.YELLOW}{static_ip}{Style.RESET_ALL} on DHCP server.")

class VIVaConfigurator(BaseConfigurator):
    def __init__(self):
        super().__init__()
        self.username = "root"
        self.password = "vmware"
        self.external_gateway = "192.168.4.7"
        self.external_dns = "10.241.96.14"
        self.internal_gateway = "192.168.4.1"
        self.internal_dns = "192.168.4.1"
        self.subnet_mask = "22" # 255.255.252.0

    def check_internet(self, ssh) -> bool:
        """Check internet connectivity"""
        print("\nVerifying Internet connectivity...")
        try:
            cmd = 'wget --spider --timeout=5 www.google.com'
            stdin, stdout, stderr = ssh.exec_command(cmd)
            exit_status = stdout.channel.recv_exit_status()
            
            if exit_status == 0:
                return True
            else:
                error = stderr.read().decode()
                print(f"{Fore.RED}Network check failed with error: {error}{Style.RESET_ALL}")
                return False
        except Exception as e:
            print(f"{Fore.RED}Error checking internet connection: {str(e)}{Style.RESET_ALL}")
            return False

    def configure_hosts_file(self, ssh_client, internal_ip: str) -> bool:
        """Configure /etc/hosts file on VIVa"""
        try:
            stdin, stdout, stderr = ssh_client.exec_command(
                f"grep -q '{internal_ip} cert-viva-local' /etc/hosts"
            )
            if stdout.channel.recv_exit_status() != 0:
                command = f"""sudo sed -i '/# End/i {internal_ip} cert-viva-local' /etc/hosts"""
                stdin, stdout, stderr = ssh_client.exec_command(command)
                if stdout.channel.recv_exit_status() != 0:
                    print(f"{Fore.RED}Failed to modify /etc/hosts{Style.RESET_ALL}")
                    return False
            return True
        except Exception as e:
            print(f"{Fore.RED}Error configuring hosts file: {e}{Style.RESET_ALL}")
            return False

    def configure_external_network_config(self, ssh_client, external_ip: str) -> tuple:
        """Configure external network settings"""
        network_config = f"""[Match]
Name=e*

[Network]
DHCP=no
Address={external_ip}/{self.subnet_mask}
Gateway={self.external_gateway}
DNS={self.external_dns}
IP6AcceptRA=no

[DHCPv4]
SendRelease=no
"""
        try:
            print("\nConfiguring /etc/systemd/network/99-dhcp-en.network for external network...")
            sftp = ssh_client.open_sftp()
            with sftp.file('/etc/systemd/network/99-dhcp-en.network', 'w') as f:
                f.write(network_config)
            sftp.close()
            
            stdin, stdout, stderr = ssh_client.exec_command(
                "sudo systemctl restart systemd-networkd"
            )
            
            ssh_client.close()
            
            print("Waiting for network service to restart...")
            time.sleep(10)
            
            new_ssh_client = self.ssh_connect(external_ip, self.username, self.password)
            if new_ssh_client:
                return True, new_ssh_client
            return False, None
        except Exception as e:
            print(f"{Fore.RED}External network configuration error: {e}{Style.RESET_ALL}")
            return False, None

    def set_hostname(self, ssh_client) -> bool:
        """Set hostname to photon-viva"""
        try:
            stdin, stdout, stderr = ssh_client.exec_command(
                "hostnamectl set-hostname photon-viva"
            )
            if stdout.channel.recv_exit_status() != 0:
                print(f"{Fore.RED}Failed to set hostname{Style.RESET_ALL}")
                return False
            return True
        except Exception as e:
            print(f"{Fore.RED}Error setting hostname: {e}{Style.RESET_ALL}")
            return False

    def configure_internal_network_config(self, ssh_client, internal_ip: str) -> tuple:
        """Configure internal network settings"""
        network_config = f"""[Match]
Name=e*

[Network]
DHCP=no
Address={internal_ip}/{self.subnet_mask}
Gateway={self.internal_gateway}
DNS={self.internal_dns}
IP6AcceptRA=no

[DHCPv4]
SendRelease=no
"""
        try:
            print("\nConfiguring /etc/systemd/network/99-dhcp-en.network for internal network...")
            sftp = ssh_client.open_sftp()
            with sftp.file('/etc/systemd/network/99-dhcp-en.network', 'w') as f:
                f.write(network_config)
            sftp.close()
            
            stdin, stdout, stderr = ssh_client.exec_command(
                "sudo systemctl restart systemd-networkd"
            )
            
            ssh_client.close()
            
            print("Waiting for network service to restart...")
            time.sleep(10)
            
            new_ssh_client = self.ssh_connect(internal_ip, self.username, self.password)
            if new_ssh_client:
                return True, new_ssh_client
            return False, None
        except Exception as e:
            print(f"{Fore.RED}Internal network configuration error: {e}{Style.RESET_ALL}")
            return False, None

    def update_jump_server_hosts_file(self, internal_ip: str) -> bool:
        """Update local hosts file with VIVa entry"""
        hosts_file = r"C:\Windows\System32\drivers\etc\hosts"
        new_entry = f"{internal_ip} cert-viva-local"
        
        try:
            print(f"\nUpdating {hosts_file} for jump server")
            
            with open(hosts_file, 'r') as f:
                lines = f.readlines()
            
            filtered_lines = [line for line in lines if 'cert-viva-local' not in line]
            
            with open(hosts_file, 'w') as f:
                f.writelines(filtered_lines)
                f.write(f"\n{new_entry}")
            return True
        except PermissionError:
            print(f"{Fore.RED}Permission denied. Please run the script as administrator{Style.RESET_ALL}")
            return False
        except Exception as e:
            print(f"{Fore.RED}Error updating hosts file: {e}{Style.RESET_ALL}")
            return False

    def configure_network(self, ssh_client, new_ip, subnet_mask, gateway):
        """VIVa specific network configuration"""
        # VIVa specific implementation
        pass

    def configure(self):
        print(
        f"""{Fore.CYAN}
 ___________________________________________________________________
 <Prerequisites>
 To move forward, make sure you've already completed the following:
    1. Downloaded the 'viva-xxxx.ova' from Broadcom TAP website
    2. Deployed the 'viva-xxxx.ova' on TC
    3. Obtained the DHCP IP address of VIVa from TC
 ___________________________________________________________________{Style.RESET_ALL}
    """
        )

        # 獲取內部 IP
        while True:
            internal_ip = input("Enter VIVa IP address: ").strip()
            if self.validate_ip(internal_ip):
                if self.ping_check(internal_ip):
                    break
                else:
                    print(f"{Fore.RED}Failed to ping IP{Style.RESET_ALL}")
            else:
                print(f"{Fore.YELLOW}Invalid IP format{Style.RESET_ALL}")

        # 建立 SSH 連接
        ssh_client = self.ssh_connect(internal_ip, self.username, self.password)
        if not ssh_client:
            return

        # 設定主機名稱
        print(f"\nSetting hostname to photon-viva...")
        if not self.set_hostname(ssh_client):
            ssh_client.close()
            return
        print(f"{Fore.GREEN}Hostname configuration successful{Style.RESET_ALL}\n")

        # 配置 hosts 檔案
        print(f"\nConfiguring /etc/hosts...")
        if not self.configure_hosts_file(ssh_client, internal_ip):
            ssh_client.close()
            return
        print(f"{Fore.GREEN}Hosts file configuration successful{Style.RESET_ALL}\n\n")

        # 獲取外部 IP
        external_ip = internal_ip
        print(f"\nUsing internal IP {Fore.CYAN}{external_ip}{Style.RESET_ALL} for Internet access")

        # 配置外部網路
        success, new_ssh_client = self.configure_external_network_config(ssh_client, external_ip)
        if success and new_ssh_client:
            print(f"{Fore.GREEN}External network configuration successful{Style.RESET_ALL}\n")
            ssh_client = new_ssh_client
            
            if self.check_internet(ssh_client):
                print(f"{Fore.GREEN}Internet connectivity check successful{Style.RESET_ALL}\n\n")

                print("Refreshing VIVA service...")
                stdin, stdout, stderr = ssh_client.exec_command(
                    "bash /opt/broadcom/viva/refresh_viva_service.sh"
                )
                if stdout.channel.recv_exit_status() == 0:
                    print(f"{Fore.GREEN}VIVA service refresh successful{Style.RESET_ALL}\n")
                    
                    success, new_ssh_client = self.configure_internal_network_config(
                        ssh_client, internal_ip
                    )
                    if success and new_ssh_client:
                        print(f"{Fore.GREEN}Internal network configuration successful{Style.RESET_ALL}\n")
                        ssh_client = new_ssh_client
                        
                        ssh_client.close()
                        
                        if self.update_jump_server_hosts_file(internal_ip):
                            print(f"{Fore.GREEN}Jump server hosts file configuration successful{Style.RESET_ALL}")
                        else:
                            print(f"{Fore.RED}Failed to update jump server hosts file{Style.RESET_ALL}")
                    else:
                        print(f"{Fore.RED}Internal network configuration failed{Style.RESET_ALL}\n")
                else:
                    print(f"{Fore.RED}Failed to refresh VIVa service{Style.RESET_ALL}\n")
                    
                print(f"\n\n\n{Fore.GREEN}***************************************{Style.RESET_ALL}")
                print(f"{Fore.GREEN}All configurations have been completed!{Style.RESET_ALL}")
                
                url = "http://cert-viva-local/Certs"
                pyperclip.copy(url)
                print(f"\nEnsure the jump server has Internet connectivity, then open your browser to visit {Fore.CYAN}{url}{Style.RESET_ALL}.")
                print(f"\nDownload the {Fore.CYAN}Agent image (.ova){Style.RESET_ALL} and {Fore.CYAN}Runlist (.json){Style.RESET_ALL} after filling in all the required data on the web UI")
            else:
                print(f"{Fore.RED}Internet connectivity check failed{Style.RESET_ALL}\n")
        else:
            print(f"{Fore.RED}Network configuration failed{Style.RESET_ALL}\n")

class AgentConfigurator(BaseConfigurator):
    def __init__(self):
        super().__init__()
        self.username = "root"
        self.password = "vmware"
        self.external_gateway = "192.168.4.7"
        self.external_dns = "10.241.96.14"
        self.internal_gateway = "192.168.4.1"
        self.internal_dns = "192.168.4.1"
        self.subnet_mask = "22" # 255.255.252.0

    def check_internet(self, ssh) -> bool:
        """Check internet connectivity"""
        print("\nVerifying Internet connectivity...")
        try:
            cmd = 'wget --spider --timeout=5 www.google.com'
            stdin, stdout, stderr = ssh.exec_command(cmd)
            exit_status = stdout.channel.recv_exit_status()
            
            if exit_status == 0:
                return True
            else:
                error = stderr.read().decode()
                print(f"{Fore.RED}Network check failed with error: {error}{Style.RESET_ALL}")
                return False
        except Exception as e:
            print(f"{Fore.RED}Error checking internet connection: {str(e)}{Style.RESET_ALL}")
            return False

    def configure_external_network_config(self, ssh_client, external_ip: str) -> tuple:
        """Configure external network settings"""
        network_config = f"""[Match]
Name=e*

[Network]
DHCP=no
Address={external_ip}/{self.subnet_mask}
Gateway={self.external_gateway}
DNS={self.external_dns}
"""
        try:
            print("\nConfiguring /etc/systemd/network/99-dhcp-en.network for external network...")
            sftp = ssh_client.open_sftp()
            with sftp.file('/etc/systemd/network/99-dhcp-en.network', 'w') as f:
                f.write(network_config)
            sftp.close()

            # Agent無法使用sudo
            stdin, stdout, stderr = ssh_client.exec_command(
                "systemctl restart systemd-networkd"
            )
            
            ssh_client.close()
            
            print("Waiting for network service to restart...")
            time.sleep(10)
            
            new_ssh_client = self.ssh_connect(external_ip, self.username, self.password)
            if new_ssh_client:
                return True, new_ssh_client
            return False, None
        except Exception as e:
            print(f"{Fore.RED}External network configuration error: {e}{Style.RESET_ALL}")
            return False, None

    def configure_internal_network_config(self, ssh_client, internal_ip: str) -> tuple:
        """Configure internal network settings"""
        network_config = f"""[Match]
Name=e*

[Network]
DHCP=no
Address={internal_ip}/{self.subnet_mask}
Gateway={self.internal_gateway}
DNS={self.internal_dns}
"""
        try:
            print("\nConfiguring /etc/systemd/network/99-dhcp-en.network for internal network...")
            sftp = ssh_client.open_sftp()
            with sftp.file('/etc/systemd/network/99-dhcp-en.network', 'w') as f:
                f.write(network_config)
            sftp.close()
            
            stdin, stdout, stderr = ssh_client.exec_command(
                "sudo systemctl restart systemd-networkd"
            )
            
            ssh_client.close()
            
            print("Waiting for network service to restart...")
            time.sleep(10)
            
            new_ssh_client = self.ssh_connect(internal_ip, self.username, self.password)
            if new_ssh_client:
                return True, new_ssh_client
            return False, None
        except Exception as e:
            print(f"{Fore.RED}Internal network configuration error: {e}{Style.RESET_ALL}")
            return False, None

    def configure_network(self, ssh_client, new_ip, subnet_mask, gateway):
        """Agent specific network configuration"""
        # Agent specific implementation
        pass

    def configure(self):
        print(
        f"""{Fore.CYAN}
 ___________________________________________________________________
 <Prerequisites>
 To move forward, make sure you've already completed the following:
    1. Downloaded the Agent image (.ova) and Runlist (.json) from VIVa
    2. Placed the runlist.json in the current directory
    3. Deployed the agent image on TC
    4. Obtained the DHCP IP address of Agent from TC
 ___________________________________________________________________{Style.RESET_ALL}
    """
        )

        # 獲取內部 IP 並建立 SSH 連接
        ssh_client = None
        while True:
            internal_ip = input("Enter Agent IP address: ").strip()
            if self.validate_ip(internal_ip):
                # Agent預設關閉ICMP阻止外部ping請求，直接嘗試建立 SSH 連接，不進行 ping 檢查
                ssh_client = self.ssh_connect(internal_ip, self.username, self.password)
                if ssh_client:
                    break
                else:
                    print(f"{Fore.RED}Failed to connect to Agent. Please check the IP address and ensure Agent's SSH is enabled.{Style.RESET_ALL}")
            else:
                print(f"{Fore.YELLOW}Invalid IP format{Style.RESET_ALL}")

        if not ssh_client:
            return

        # 檢查並傳送 runlist.json
        current_dir = os.getcwd()
        runlist_path = os.path.join(current_dir, "runlist.json")
        print("\nTransferring runlist to Agent...")
        if not os.path.exists(runlist_path):
            print(f"{Fore.YELLOW}runlist.json not found in the current directory.{Style.RESET_ALL}")
        
        while True:
            if not os.path.exists(runlist_path):
                while True:
                    response = input("Please place runlist.json in the current directory to continue (y/n): ").strip().lower()
                    if response in ['y', 'n']:
                        break
                
                if response == 'y':
                    # 重新檢查檔案是否存在
                    if os.path.exists(runlist_path):
                        break
                elif response == 'n':
                    ssh_client.close()
                    return
            else:
                break

        # 檢查遠端是否存在runlist.json
        try:
            sftp = ssh_client.open_sftp()
            try:
                sftp.stat('/vmware/input/runlist.json')
                while True:
                    response = input(f"{Fore.YELLOW}runlist.json already exists on the remote host. Overwrite? (y/n): {Style.RESET_ALL}").strip().lower()
                    if response in ['y', 'n']:
                        break
                
                if response != 'y':
                    print("Skipping file transfer...\n")
                    sftp.close()
                else:
                    # Transfer file if user chooses to overwrite
                    sftp.put(runlist_path, '/vmware/input/runlist.json')
                    print(f"{Fore.GREEN}runlist.json transferred successfully{Style.RESET_ALL}\n\n")
                    sftp.close()
            except FileNotFoundError:
                # Transfer file if it doesn't exist
                sftp.put(runlist_path, '/vmware/input/runlist.json')
                print(f"{Fore.GREEN}runlist.json transferred successfully{Style.RESET_ALL}\n\n")
                sftp.close()
        except Exception as e:
            print(f"{Fore.RED}Error transferring runlist.json: {e}{Style.RESET_ALL}\n")
            ssh_client.close()
            return

        # 直接使用內部 IP當作外部 IP
        external_ip = internal_ip
        print(f"\nUsing internal IP {Fore.CYAN}{external_ip}{Style.RESET_ALL} for Internet access")

        # 配置外部網路
        success, new_ssh_client = self.configure_external_network_config(ssh_client, external_ip)
        if success and new_ssh_client:
            print(f"{Fore.GREEN}External network configuration successful{Style.RESET_ALL}\n")
            ssh_client = new_ssh_client
            
            if self.check_internet(ssh_client):
                print(f"{Fore.GREEN}Internet connectivity check successful{Style.RESET_ALL}\n\n")

                print("Running AgentLauncher...")
                stdin, stdout, stderr = ssh_client.exec_command("AgentLauncher -i")

                # 追蹤已完成的docker image層數
                completed_layers = 0
                total_layers = 0

                for line in stdout:
                    line = line.strip()
                    if "Pulling fs layer" in line:
                        total_layers += 1
                    elif "Pull complete" in line:
                        completed_layers += 1
                        print(f"\rProgress: {completed_layers}/{total_layers} layers completed", end='', flush=True)

                # 最後打印一個換行
                print()

                # 檢查錯誤
                for line in stderr:
                    print(f"Error: {line.strip()}")
                    
                success, new_ssh_client = self.configure_internal_network_config(
                    ssh_client, internal_ip
                )
                if success and new_ssh_client:
                    print(f"{Fore.GREEN}Internal network configuration successful{Style.RESET_ALL}\n")
                    ssh_client = new_ssh_client
                    ssh_client.close()
                    
                    print(f"\n\n\n{Fore.GREEN}***************************************{Style.RESET_ALL}")
                    print(f"{Fore.GREEN}All configurations have been completed!{Style.RESET_ALL}")
                    url = f"https://{internal_ip}/agent-ui"
                    pyperclip.copy(url)
                    print(f"\nOpen your browser to visit {Fore.CYAN}{url}{Style.RESET_ALL} for Agent web UI access.")
            
                else:
                    print(f"{Fore.RED}Internal network configuration failed{Style.RESET_ALL}\n")
            else:
                print(f"{Fore.RED}Internet connectivity check failed{Style.RESET_ALL}\n")
        else:
            print(f"{Fore.RED}Network configuration failed{Style.RESET_ALL}\n")

class PciPassthruConfigurator(BaseConfigurator):
    def __init__(self):
        super().__init__()
        self.user = "root"           # ESXi 使用者
        self.password = "Admin!23"   # ESXi 密碼

    def add_vm_options(self, si, vm_name):
        """添加 PCI Passthrough 選項到 VM"""
        try:
            content = si.RetrieveContent()
            
            # 找到虛擬機
            vm = None
            container = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
            for managed_object in container.view:
                if managed_object.name == vm_name:
                    vm = managed_object
                    break
            container.Destroy()
            
            if not vm:
                print(f"{Fore.RED}VM name: '{vm_name}' is not found{Style.RESET_ALL}")
                return

            # 找到主機系統
            host = vm.runtime.host
            
            # 尋找 NVIDIA GPU PCI 設備
            nvidia_device = None
            for pci_dev in host.hardware.pciDevice:
                if "NVIDIA" in pci_dev.vendorName:
                    nvidia_device = pci_dev
                    print(f"\nFound NVIDIA GPU:")
                    print(f"  Device Name: {Fore.LIGHTCYAN_EX}{pci_dev.deviceName}{Style.RESET_ALL}")
                    print(f"  Vendor Name: {Fore.LIGHTCYAN_EX}{pci_dev.vendorName}{Style.RESET_ALL}")
                    print(f"  Device ID: {Fore.LIGHTCYAN_EX}{hex(pci_dev.deviceId)}{Style.RESET_ALL}")
                    print(f"  Vendor ID: {Fore.LIGHTCYAN_EX}{hex(pci_dev.vendorId)}{Style.RESET_ALL}")
                    print(f"  PCI ID: {Fore.LIGHTCYAN_EX}{pci_dev.id}{Style.RESET_ALL}\n")
                    break
            
            if not nvidia_device:
                print(f"{Fore.RED}No NVIDIA GPU found on the host{Style.RESET_ALL}")
                return

            # 啟用 PCI 設備的 passthrough
            passthru_sys = host.configManager.pciPassthruSystem
            if passthru_sys:
                # 創建 passthrough 配置
                config = vim.host.PciPassthruConfig()
                config.id = nvidia_device.id
                config.passthruEnabled = True
                
                try:
                    # 更新 PCI passthrough 配置
                    passthru_sys.UpdatePassthruConfig([config])
                    print(f"{Fore.GREEN}Successfully updated PCI passthrough configuration{Style.RESET_ALL}")
                except Exception as e:
                    print(f"{Fore.RED}Failed to update PCI passthrough config: {e}{Style.RESET_ALL}")
                    return
            
            try:
                # 創建 VM 配置
                vm_config_spec = vim.vm.ConfigSpec()
                
                # 添加 PCI passthrough 設備
                pci_spec = vim.vm.device.VirtualDeviceSpec()
                pci_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
                
                pci_device = vim.vm.device.VirtualPCIPassthrough()
                pci_device.backing = vim.vm.device.VirtualPCIPassthrough.DeviceBackingInfo()
                pci_device.backing.id = nvidia_device.id
                pci_device.backing.deviceId = hex(nvidia_device.deviceId)[2:].zfill(4)
                pci_device.backing.systemId = host.hardware.systemInfo.uuid
                pci_device.backing.vendorId = nvidia_device.vendorId
                
                # 設置設備的鍵值
                pci_device.key = -1  # 讓 vSphere 自動分配鍵值
                
                pci_spec.device = pci_device
                
                # 更新設備配置列表
                vm_config_spec.deviceChange = [pci_spec]
                
                print(f"\nAttempting to add PCI device to VM {Fore.CYAN}'{vm_name}'{Style.RESET_ALL}...")
                
                # 重新配置 VM
                task = vm.ReconfigVM_Task(spec=vm_config_spec)
                
                # 等待任務完成
                while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                    pass
                
                if task.info.state == vim.TaskInfo.State.success:
                    print(f"{Fore.GREEN}Successfully added PCI passthrough device to '{vm_name}'{Style.RESET_ALL}")
                    
                    # 添加 VM 選項
                    try:
                        vm_config_spec = vim.vm.ConfigSpec()
                        vm_config_spec.extraConfig = [
                            vim.option.OptionValue(key='pciHole.start', value='2048'),
                            vim.option.OptionValue(key='pciPassthru.use64bitMMIO', value='TRUE'),
                            vim.option.OptionValue(key='pciPassthru.64bitMMIOSizeGB', value='256')
                        ]
                        
                        # 添加記憶體預留鎖定選項
                        vm_config_spec.memoryReservationLockedToMax = True
                        
                        print(f"\nAdding VM options to {Fore.CYAN}'{vm_name}'{Style.RESET_ALL}...")
                        
                        # 重新配置 VM
                        task = vm.ReconfigVM_Task(spec=vm_config_spec)
                        
                        # 等待任務完成
                        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                            pass
                        
                        if task.info.state == vim.TaskInfo.State.success:
                            print(f"{Fore.GREEN}Successfully added VM options to '{vm_name}'{Style.RESET_ALL}")
                        else:
                            print(f"{Fore.RED}Failed to add VM options: {task.info.error}{Style.RESET_ALL}")
                            
                    except Exception as e:
                        print(f"{Fore.RED}Error adding VM options: {e}{Style.RESET_ALL}")
                else:
                    print(f"{Fore.RED}Failed to add PCI passthrough device to VM: {task.info.error}{Style.RESET_ALL}")
                    return
                    
            except Exception as e:
                print(f"{Fore.RED}Error adding PCI device to VM: {e}{Style.RESET_ALL}")
                return
                
        except Exception as e:
            print(f"{Fore.RED}Error adding VM options: {e}{Style.RESET_ALL}")

    def configure_network(self, ssh_client, new_ip, subnet_mask, gateway):
        """PCI Passthrough 不需要網路配置"""
        pass

    def configure(self):
        print(
        f"""{Fore.CYAN}
 ___________________________________________________________________
 <Prerequisites>
 To move forward, make sure you've already completed the following:
    1. Installed NVIDIA GPU on the target VM
    2. Obtained the IP address of the target VM
 ___________________________________________________________________{Style.RESET_ALL}
    """
        )

        try:
            while True:
                host = self.get_valid_input("Enter Host IP address: ")
                if host:
                    if not self.validate_ip(host):
                        print(f"{Fore.YELLOW}Invalid IP format{Style.RESET_ALL}")
                        continue
                    # 測試連線
                    connected, self.si = self.test_connection(host, self.user, self.password)
                    if not connected:
                        continue
                    break
            
            # 列出可用的VM
            vm_list = self.list_vms(self.si)
            if vm_list:
                while True:
                    vm_input = input("\nEnter VM number (or 'q' to quit): ").strip()
                    if vm_input.lower() == 'q':
                        break
                    
                    try:
                        # 嘗試將輸入轉換為數字
                        idx = int(vm_input) - 1
                        if 0 <= idx < len(vm_list):
                            self.vm_name = vm_list[idx]
                            break
                        else:
                            print(f"{Fore.YELLOW}Please enter a number between 1 and {len(vm_list)}{Style.RESET_ALL}")
                    except ValueError:
                        print(f"{Fore.YELLOW}Please enter a valid number{Style.RESET_ALL}")
                
                if self.vm_name:
                    self.add_vm_options(self.si, self.vm_name)
                    

        except KeyboardInterrupt:
            print(f"\n{Fore.YELLOW}Program interrupted by user{Style.RESET_ALL}")
        except Exception as e:
            print(f"\n{Fore.RED}An error occurred: {e}{Style.RESET_ALL}")
        finally:
            # 確保連接被正確關閉
            if self.si:
                Disconnect(self.si)

class OVFManager(BaseConfigurator):
    def __init__(self):
        super().__init__()
        self.user = "root"           # ESXi 使用者
        self.password = "Admin!23"   # ESXi 密碼
        self.datastore = "datastore1"
        self.ovf_tool_path = r"C:\Program Files\VMware\VMware OVF Tool\ovftool.exe"

    def check_ovf_tool(self) -> bool:
        """檢查 OVF Tool 是否已安裝"""
        print("\nVerifying if VMware OVF tool is installed...")
        if os.path.exists(self.ovf_tool_path):
            print(f"{Fore.GREEN}VMware OVF Tool has been installed{Style.RESET_ALL}\n")
            return True
        else:
            print(f"{Fore.YELLOW}VMware OVF Tool is not installed{Style.RESET_ALL}\n")
            return False

    def list_ovf_files(self) -> list:
        """列出當前目錄中的所有 OVF/OVA 文件"""
        ovf_files = []
        for file in os.listdir('.'):
            if file.lower().endswith(('.ovf', '.ova')):
                file_path = os.path.join(os.getcwd(), file)
                create_time = os.path.getctime(file_path)
                ovf_files.append((file, create_time))
        
        if not ovf_files:
            return []
        
        # 按時間排序，最新的在最後
        ovf_files.sort(key=lambda x: x[1])
        return [file[0] for file in ovf_files]

    def select_ovf_file(self) -> str:
        """讓用戶選擇 OVF 文件"""
        while True:
            ovf_files = self.list_ovf_files()
            
            if not ovf_files:
                print(f"{Fore.YELLOW}No OVF/OVA files found in current directory{Style.RESET_ALL}")
                while True:
                    response = input("Please place OVF file in the current directory to continue (y/n): ").strip().lower()
                    if response in ['y', 'n']:
                        break
                
                if response == 'n':
                    return None
                # 如果選擇 'y'，繼續迴圈檢查檔案是否存在
                continue
            
            if len(ovf_files) == 1:
                return ovf_files[0]
            
            print("\nAvailable OVF file(s) (oldest to newest):")
            for i, file in enumerate(ovf_files, 1):
                print(f"{i}) {file}")
            
            while True:
                try:
                    choice = int(input("Enter file number: ")) - 1
                    if 0 <= choice < len(ovf_files):
                        return ovf_files[choice]
                except ValueError:
                    pass

    def deploy_ovf(self, host: str, ovf_file: str) -> bool:
        """部署 OVF 文件到 ESXi 主機"""
        try:
            print(f"\n\nDeploying OVF file to ESXi host...")
            cmd = [
                self.ovf_tool_path,
                '--noSSLVerify',
                '--acceptAllEulas',
                '--X:logLevel=verbose',  # 顯示詳細日誌
                f'--datastore={self.datastore}',
                ovf_file,
                f'vi://{self.user}:{self.password}@{host}'
            ]
            
            # 使用 subprocess.Popen 來即時顯示輸出
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True
            )
            
            # 即時顯示輸出
            while True:
                output = process.stdout.readline()
                if output == '' and process.poll() is not None:
                    break
                if output:
                    # 如果是進度信息，在同一行顯示
                    if 'Disk progress:' in output:
                        if '99%' in output or '100%' in output:
                            print(f"\r{output.strip()}", end='', flush=True)
                            print("\n", end='', flush=True)
                        else:
                            print(f"\r{output.strip()}", end='', flush=True)
                    # 只顯示重要信息，過濾掉警告和重複的TransferCompleted
                    elif 'Warning:' not in output and 'Invalid value' not in output:
                        # 移除重複的TransferCompleted
                        if 'Transfer Completed' in output:
                            output = output.replace('Transfer CompletedTransferCompleted', 'Transfer Completed')
                        print(output.strip())
            
            # 獲取返回碼
            return_code = process.poll()
            
            if return_code == 0:
                print(f"\n{Fore.GREEN}OVF deployment successful{Style.RESET_ALL}")
                return True
            else:
                error = process.stderr.read()
                print(f"{Fore.RED}OVF deployment failed: {error}{Style.RESET_ALL}")
                return False
        except Exception as e:
            print(f"{Fore.RED}Error deploying OVF: {e}{Style.RESET_ALL}")
            return False

    def export_ovf(self, host: str, vm_name: str, output_name: str) -> bool:
        """從 ESXi 主機導出 OVF 文件"""
        try:
            # 確保輸出檔案名以 .ova 結尾
            if not output_name.lower().endswith('.ova'):
                output_name += '.ova'
            
            output_path = os.path.join(os.getcwd(), output_name)
            
            # 檢查檔案是否已存在
            if os.path.exists(output_path):
                while True:
                    response = input(f"{Fore.YELLOW}File '{output_name}' already exists. Overwrite? (y/n): {Style.RESET_ALL}").strip().lower()
                    if response in ['y', 'n']:
                        break
                
                if response != 'y':
                    print(f"{Fore.YELLOW}Export cancelled{Style.RESET_ALL}")
                    return False
            
            cmd = [
                self.ovf_tool_path,
                '--noSSLVerify',
                '--acceptAllEulas',
                '--overwrite',
                '--X:logLevel=verbose',  # 顯示詳細日誌
                f'vi://{self.user}:{self.password}@{host}/{vm_name}',
                output_path
            ]
            
            print(f"\n\nExporting OVF file from ESXi host...")
            
            # 使用 subprocess.Popen 來即時顯示輸出
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True
            )
            
            # 即時顯示輸出
            while True:
                output = process.stdout.readline()
                if output == '' and process.poll() is not None:
                    break
                if output:
                    # 如果是進度信息，在同一行顯示
                    if 'Disk progress:' in output:
                        if '99%' in output or '100%' in output:
                            print(f"\r{output.strip()}", end='', flush=True)
                            print("\n", end='', flush=True)
                        else:
                            print(f"\r{output.strip()}", end='', flush=True)
                    # 只顯示重要信息，過濾掉警告和重複的TransferCompleted
                    elif 'Warning:' not in output and 'Invalid value' not in output:
                        # 移除重複的TransferCompleted
                        if 'Transfer Completed' in output:
                            output = output.replace('Transfer CompletedTransferCompleted', 'Transfer Completed')
                        print(output.strip())
            
            # 獲取返回碼
            return_code = process.poll()
            
            if return_code == 0:
                print(f"\n{Fore.GREEN}'{output_name}' exported successfully{Style.RESET_ALL}\n")
                return True
            else:
                error = process.stderr.read()
                print(f"{Fore.RED}OVF export failed: {error}{Style.RESET_ALL}\n")
                return False
        except Exception as e:
            print(f"{Fore.RED}Error exporting OVF: {e}{Style.RESET_ALL}\n")
            return False

    def delete_vm(self, vm_name):
        """刪除指定的虛擬機"""
        try:
            content = self.si.RetrieveContent()
            
            # 找到虛擬機
            vm = None
            container = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
            for managed_object in container.view:
                if managed_object.name == vm_name:
                    vm = managed_object
                    break
            container.Destroy()
            
            if not vm:
                print(f"{Fore.RED}VM '{vm_name}' not found{Style.RESET_ALL}")
                return
            
            # 檢查VM電源狀態
            if vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOn:
                print(f"{Fore.YELLOW}VM '{vm_name}' is currently powered on{Style.RESET_ALL}")
                while True:
                    power_off = input("Would you like to power off the VM before deletion? (y/n): ").upper()
                    if power_off == 'Y':
                        print(f"Powering off VM '{vm_name}'...")
                        task = vm.PowerOffVM_Task()
                        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                            pass
                        if task.info.state != vim.TaskInfo.State.success:
                            print(f"{Fore.RED}Failed to power off VM: {task.info.error}{Style.RESET_ALL}")
                            return
                        print(f"{Fore.GREEN}VM powered off successfully{Style.RESET_ALL}")
                        break
                    elif power_off == 'N':
                        print(f"{Fore.YELLOW}Cannot delete a powered on VM. Operation cancelled.{Style.RESET_ALL}")
                        return
                    else:
                        print(f"{Fore.YELLOW}Please enter Y or N{Style.RESET_ALL}")
                        continue
            
            # 確認刪除
            while True:
                confirm = input(f"Are you sure you want to delete VM {Fore.CYAN}'{vm_name}'{Style.RESET_ALL}? (y/n): ").upper()
                if confirm == 'Y':
                    break
                elif confirm == 'N':
                    print(f"{Fore.YELLOW}Delete operation cancelled{Style.RESET_ALL}")
                    return
                else:
                    print(f"{Fore.YELLOW}Please enter Y or N{Style.RESET_ALL}")
                    continue
                
            # 執行刪除任務
            task = vm.Destroy_Task()
            print(f"\nDeleting VM '{vm_name}'...")
            
            # 等待任務完成
            while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                pass
            
            if task.info.state == vim.TaskInfo.State.success:
                print(f"{Fore.GREEN}VM '{vm_name}' deleted successfully{Style.RESET_ALL}")
            else:
                print(f"{Fore.RED}Failed to delete VM: {task.info.error}{Style.RESET_ALL}")
                
        except Exception as e:
            print(f"{Fore.RED}Error deleting VM: {e}{Style.RESET_ALL}")

    def configure_network(self, ssh_client, new_ip, subnet_mask, gateway):
        """OVF Manager 不需要網路配置"""
        pass

    def configure(self):
        print(
        f"""{Fore.CYAN}
 ___________________________________________________________________
 <Prerequisites>
 To move forward, make sure you've already completed the following:
    1. Installed 'VMware OVF Tool' on this machine (jump server)
    2. Obtained the IP address of the target ESXi host
    3. Placed the OVF (.ovf or .ova) file in the current directory (for Deploy use)
 ___________________________________________________________________{Style.RESET_ALL}
    """
        )

        if not self.check_ovf_tool():
            return

        print("\n1) Deploy OVF to ESXi\n2) Export OVF from ESXi\n3) Delete VM from ESXi")
        while True:
            choice = input("\nEnter your choice (1-3): ").strip()
            if choice in ['1', '2', '3']:
                break

        try:
            while True:
                host = self.get_valid_input("\nEnter Host IP address: ")
                if host:
                    if not self.validate_ip(host):
                        print(f"{Fore.YELLOW}Invalid IP format{Style.RESET_ALL}")
                        continue
                    # 測試連線
                    connected, self.si = self.test_connection(host, self.user, self.password)
                    if not connected:
                        continue
                    break

            if choice == '1': # Deploy OVF
                ovf_file = self.select_ovf_file()
                if ovf_file:
                    self.deploy_ovf(host, ovf_file)
            elif choice == '2': # Export OVF
                vm_list = self.list_vms(self.si)
                if vm_list:
                    while True:
                        vm_input = input("\nEnter VM number (or 'q' to quit): ").strip()
                        if vm_input.lower() == 'q':
                            break
                        
                        try:
                            # 嘗試將輸入轉換為數字
                            idx = int(vm_input) - 1
                            if 0 <= idx < len(vm_list):
                                vm_name = vm_list[idx]
                                break
                        except ValueError:
                            print(f"{Fore.YELLOW}Please enter a valid number{Style.RESET_ALL}")
                    
                    if vm_input.lower() != 'q':
                        output_name = input("Enter output OVF file name: ").strip()
                        if output_name:
                            self.export_ovf(host, vm_name, output_name)
            elif choice == '3': # Delete VM
                vm_list = self.list_vms(self.si)
                if vm_list:
                    while True:
                        vm_input = input("\nEnter VM number (or 'q' to quit): ").strip()
                        if vm_input.lower() == 'q':
                            break
                        
                        try:
                            # 嘗試將輸入轉換為數字
                            idx = int(vm_input) - 1
                            if 0 <= idx < len(vm_list):
                                vm_name = vm_list[idx]
                                break
                        except ValueError:
                            print(f"{Fore.YELLOW}Please enter a valid number{Style.RESET_ALL}")
                    
                    if vm_input.lower() != 'q':
                        self.delete_vm(vm_name)

        except KeyboardInterrupt:
            print(f"\n{Fore.YELLOW}Operation cancelled by user{Style.RESET_ALL}")
        except Exception as e:
            print(f"\n{Fore.RED}An error occurred: {e}{Style.RESET_ALL}")
        finally:
            if self.si:
                Disconnect(self.si)

def show_menu():
    print(
    r"""
 =======================================
 VMware Cert Test Environment Setup Tool 
                 v1.1 
 =======================================

Please select an option:
1) Config SUT
2) Config VIVa
3) Config Agent
4) Add PCI Passthrough VM Options
5) Manage OVF
6) Exit

"""
    )
    while True:
        choice = input("Enter your choice (1-6): ").strip()
        if choice in ['1', '2', '3', '4', '5', '6']:
            return choice

def main():
    while True:
        choice = show_menu()
        
        if choice == '6':
            print("\nExiting...")
            break
            
        try:
            if choice == '1':
                configurator = SUTConfigurator()
                configurator.configure()
            elif choice == '2':
                configurator = VIVaConfigurator()
                configurator.configure()
            elif choice == '3':
                configurator = AgentConfigurator()
                configurator.configure()
            elif choice == '4':
                configurator = PciPassthruConfigurator()
                configurator.configure()
            elif choice == '5':
                configurator = OVFManager()
                configurator.configure()
        except KeyboardInterrupt:
            print("\n\nOperation cancelled by user.")
        except Exception as e:
            print(f"{Fore.RED}An error occurred: {e}{Style.RESET_ALL}")
        
        input(f"\n{Fore.LIGHTBLACK_EX}\n<press Enter to return to the main menu...>{Style.RESET_ALL}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user.")
        sys.exit(0)