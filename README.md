## 业务流程
一、飞书表格行点击推送按钮->调用批量推送接口（POST 参数为tableId:飞书表格的table_id）->接口api获取表格中所有数据->
    1）获取数据并过滤表格中同步状态不是推送成功的数据->调用导入产品接口（/open/jushuitan/orders/upload）->数据响应后成功的数据同步状态修改为推送成功，失败的数据修改为同步失败并填写失败原因，无论成功失败同步时间也要填上目前同步后的时间（当前服务器时间）
    2）获取数据并过滤表格中同步状态为推送成功且快递单号为空的数据->调用发货信息查询->返回的快递单号回写到当前表格快递单号列中
    获取数据的时候注意：需要通过订单编号去重；前端可能会频繁点击，后端做防抖处理（防止频繁重复请求）；token过期前更新token，token过期后获取新的token；更新表格中状态、时间、失败原因需要根据订单号所在行更新

二、每个月6号 调用聚水潭订单查询接口 汇总数据回写到电商营收登记表格中(表格中数据不是替换，是追加的)，表格数据如下（每月一个产品汇总成一条，销量和金额累加计算），飞书表格字段显示如下：
销售日期：年月
商品名称、
69码、
销量、
金额、



## 开发基本信息
飞书：
appID:  cli_aaaacccd34545
Secret: feishu123jlk455435lkj345435

聚水潭
APP Key:   234324sdffdsa435435345
APP Secret:  345435435gfdgsfdgf43543534

文创营收登记 tableId为 23423jlkjsdfsdfsf
电商营收登记 tableId为 89kojsdsf892343kj




## 聚水潭api说明
    api调用说明文档： https://openweb.jushuitan.com/doc?docId=30
  开放平台业务接口，目前只支持HTTP POST一种请求方式。 指定Body Content-Type为：application/x-www-form-urlencoded;charset=UTF-8
 Post请求时，所有参数均通过表单传递，请勿将请求参数放到Query参数中。
    - 聚水潭公共请求参数
    app_key	POP分配给应用的app_key
    access_token 通过code获取的access_token
    timestamp UNIX时间戳，单位秒，需要与聚水潭服务器时间差值在10分钟内
    charset	 字符编码（固定值：utf-8）
    version	 版本号，固定传2
    sign 数字签名
    biz	业务请求参数，格式为jsonString，详细说明见下方【业务参数】。该参数为必填参数，如果接口不需要业务参数，请传空json对象字符串 "{}"	{"page_index":1,"page_size":50}	必填

### 获取token
 https://openapi.jushuitan.com/openWeb/auth/getInitToken （文档说明：https://openweb.jushuitan.com/doc?docId=23#4420dc60cdb74c92bf5f2754f2ea6a2e）
 参数:
 app_key	string	开发者应用Key	xxxxx	必填
timestamp	string	当前请求的时间戳【单位是秒】	1577771730	必填
grant_type	string	固定值：authorization_code	authorization_code	必填
charset	string	交互数据的编码【utf-8】目前只能传utf-8，不能不传！	utf-8	必填
code	string	随机码（随机创建六位字符串）自定义值	SLMDWBG	必填
sign	string	请求的数字签名，是通过所有请求参数通过摘要生成的，保证请求参数没有被篡改。签名拼装规则参考：https://openweb.jushuitan.com/doc?docId=70	0ecde8631431a5ed6b3e7368afbabdaoas	必填

 响应:
{
   "code": 0,
   "data": {
       "access_token": "0ecde8631431a5ed6b3e7368afbabdadss",
       "expires_in": 2592000,
       "refresh_token": "eb1964a9d142423a9f0de88b97bb38fc",
       "scope": "all"
   }
}

### 更新token
https://openapi.jushuitan.com/openWeb/auth/refreshToken （文档说明：https://openweb.jushuitan.com/doc?docId=23#4420dc60cdb74c92bf5f2754f2ea6a2e）

### 导入产品接口 
  https://openapi.jushuitan.com/open/jushuitan/orders/upload（文档说明：https://openweb.jushuitan.com/dev-doc?docType=4&docId=18）
 参数：
[
    {
        shop_id:'',//店铺编号 
        so_id:"", //自研商城系统订单号  长度<=41，自有商城店铺中唯一值不允许重复单号上传
        order_date:'', // 订单日期 （不能传订单归档日期前的日期）
        shop_status:'', 自研商城系统订单状态：等待买家付款=WAIT_BUYER_PAY，等待卖家发货=WAIT_SELLER_SEND_GOODS（传此状态时实际支付金额即pay节点支付金额=应付金额ERP才会显示已付款待审核）,等待买家确认收货=WAIT_BUYER_CONFIRM_GOODS,交易成功=TRADE_FINISHED,付款后交易关闭=TRADE_CLOSED,付款前交易关闭=TRADE_CLOSED_BY_TAOBAO；可更新
        shop_buyer_id:'',? //买家帐号 自定义上传，nvarchar(50)
        receiver_address:'', //收货地址
        receiver_name:"", //收件人
        receiver_phone:"",// 联系电话(非必填)
        pay_amount:"", //应付金额
        freight:"", //运费
        items:[//商品信息
            {
                sku_id:"",// 商品编码，对应普通商品资料页面商品编码，ERP内商品编码长度<=40PS:设置预售标识订单，修改商品编码=商品编码+预售（或==）+预计发货日期（可选）比如:"A321232"修改为"A321232预售2015-12-12"或者"A321232"修改为"A321232==2015-12-12"当商品被购买后ERP将自动识别为预售订单并更正商品编码为正确商品编码
                shop_sku_id:"", //店铺商品编码，对应店铺商品管理页面的平台店铺商品编码，店铺商品编码长度<=128，店铺商品资料未维护可自定义值上传
                amount:"", //成交总额单位（元）（（最大传2位小数））；备注：可能存在人工改价
                base_price:"", // 原价，保留两位小数，单位（元）
                price:"",//非必填 单价，单位（元）（（最大传4位小数）
                qty:"", // 数量
                name:"",//商品名称长度<=100
                outer_oi_id:"", //商家系统订单商品明细主键,为了拆单合单时溯源，最长不超过50,保持订单内唯一，支持自定义
            }
        ]
    }
]

响应：
{
    code:0,
    msg:'',
    data:{
        datas:[
            {
                o_id:"", //ERP订单界面-内部单号
                so_id:"", // ERP订单界面-线上单号
                issuccess:true, //是否成功
                msg:"", //返回结果描述
            }
        ]
    }
}



注：参数中字段对应
shop_id:'',//对应表格中渠道编码
so_id:"", //对应表格中订单编号
order_date:'', // 对应表格中下单日期
shop_status:'WAIT_SELLER_SEND_GOODS', //写死为WAIT_SELLER_SEND_GOODS
shop_buyer_id:'', //对应表格中渠道编码
receiver_address:'', //对应表格中收货地址 格式为 地址 姓名 电话（空格区分），空格拆分后第一项为地址
receiver_name:"", //对应表格中收货地址 格式为 地址 姓名 电话（空格区分），空格拆分后第二项为姓名
receiver_phone:"",//对应表格中收货地址 格式为 地址 姓名 电话（空格区分），空格拆分后第三项为电话
pay_amount:"", //对应表格中合计金额
freight:"", //对应表格中快递费
items:[//商品信息 表格中每条订单包含一个商品信息
    {
        sku_id:"", //对应表格中69码
        shop_sku_id:"", //对应表格中69码
        amount:"", //对应表格中合计金额
        base_price:"", // 对应表格中零售价
        price:"",//对应表格中折扣价
        qty:"", // 对应表格中数量
        name:"", //对应表格中商品名称
        outer_oi_id:"", //对应表格中订单编号
    }
]

### 发货信息查询
https://openapi.jushuitan.com/open/logistic/query（说明文档： https://openweb.jushuitan.com/dev-doc?docType=5&docId=25）
参数:
so_ids:[],//对应表格中订单编号数组

### 订单查询
https://openapi.jushuitan.com/open/orders/single/query (说明文档：https://openweb.jushuitan.com/dev-doc?docType=4&docId=22)
